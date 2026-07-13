"""Fail-closed Stripe subscription billing for Laura self-serve.

Hosted Checkout sells one configured Solo Price. A redirect or Checkout event
never grants avatar minutes: only a signed, exact-price subscription event may
change entitlement. Webhook claim, customer resolution and tenant mutation are
one Postgres transaction, so database failures remain retryable.
"""
from __future__ import annotations

import hmac
import re
import time
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import auth, control_plane, entitlements, store
from .config import settings

try:
    import stripe  # type: ignore
except Exception:
    stripe = None  # type: ignore

router = APIRouter(tags=["billing"])

CHECKOUT_PATH = "/billing/checkout"
PORTAL_PATH = "/billing/portal"
_EVENT_ID = re.compile(r"^evt_[A-Za-z0-9_]+$")
_ACTIVE = {"active", "trialing"}
_TERMINAL = {
    "canceled", "unpaid", "incomplete_expired", "incomplete", "paused",
}


class BillingConflict(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class InvalidSignedEvent(ValueError):
    pass


def _stripe():
    if stripe is None:
        raise RuntimeError("stripe SDK not installed")
    stripe.api_key = settings.stripe_secret_key.strip()
    stripe.api_version = settings.stripe_api_version.strip()
    return stripe


def _public_origin() -> Optional[str]:
    try:
        parsed = urlsplit(settings.public_base_url.strip())
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            return None
        host = parsed.hostname.lower()
        if parsed.port is not None and parsed.port != 443:
            host = f"{host}:{parsed.port}"
        return f"https://{host}"
    except (TypeError, ValueError):
        return None


def _mode_matches() -> bool:
    key = settings.stripe_secret_key.strip()
    prefix = "sk_live_" if settings.stripe_live_mode else "sk_test_"
    return key.startswith(prefix)


def _billing_ready() -> bool:
    if not (
        settings.billing_enabled
        and stripe is not None
        and _mode_matches()
        and settings.stripe_webhook_secret.strip().startswith("whsec_")
        and settings.stripe_price_solo.strip().startswith("price_")
        and settings.stripe_api_version.strip() == "2026-02-25.clover"
        and _public_origin()
        and control_plane.enabled()
    ):
        return False
    try:
        return bool(control_plane.billing_boundary_ready())
    except Exception:
        return False


def _not_enabled() -> JSONResponse:
    return JSONResponse({"error": "billing_not_enabled"}, status_code=404)


def _success_url() -> str:
    return f"{_public_origin()}/dashboard?checkout=success"


def _cancel_url() -> str:
    return f"{_public_origin()}/dashboard?checkout=cancel"


def _return_url() -> str:
    return f"{_public_origin()}/dashboard"


def _exact_same_origin(request: Request) -> bool:
    expected = _public_origin()
    origin = request.headers.get("origin", "").rstrip("/")
    return bool(expected and origin and hmac.compare_digest(origin, expected))


def _resolve_org(request: Request) -> Optional[str]:
    user = auth.current_user(request)
    if user is not None:
        return user["org_id"]
    provided = request.headers.get("authorization", "")
    if not provided.startswith("Bearer "):
        return None
    raw = provided[len("Bearer "):].strip()
    if not raw:
        return None
    global_token = settings.laura_api_token.strip()
    if global_token and hmac.compare_digest(raw, global_token):
        return None
    return control_plane.resolve_org_token(raw) or store.resolve_org_token(raw)


def _summary_for_org(org: Optional[str]) -> dict:
    live = _billing_ready()
    trial = int(settings.free_trial_seconds)
    base = {
        "plan": "free",
        "billing_live": live,
        "trial_seconds_total": trial,
        "included_seconds": trial,
        "used_seconds": 0,
        "remaining_seconds": trial,
        "can_start_session": True,
        "active_session": False,
        "subscription_status": "none",
        "current_period_end": None,
        "checkout_path": CHECKOUT_PATH,
        "portal_path": PORTAL_PATH,
    }
    if not control_plane.enabled() or not org:
        return base
    summary = entitlements.usage_summary(org) or {}
    billing = control_plane.get_billing(org) or {}
    included = int(summary.get("included_seconds", trial))
    used = int(summary.get("used_seconds", 0))
    remaining = int(summary.get("remaining_seconds", max(0, included - used)))
    active = entitlements.has_active_session(org)
    base.update(
        {
            "plan": str(summary.get("plan") or billing.get("plan") or "free"),
            "included_seconds": included,
            "used_seconds": used,
            "remaining_seconds": remaining,
            "can_start_session": remaining > 0 and not active,
            "active_session": active,
            "subscription_status": str(
                billing.get("subscription_status") or "none"
            ),
            "current_period_end": billing.get("current_period_end"),
        }
    )
    return base


@router.get("/billing/summary")
async def billing_summary(request: Request) -> JSONResponse:
    org = await run_in_threadpool(_resolve_org, request)
    data = await run_in_threadpool(_summary_for_org, org)
    return JSONResponse(data)


async def _billing_principal(request: Request):
    user = auth.current_user(request)
    if user is None or not _exact_same_origin(request):
        return None, JSONResponse({"error": "forbidden"}, status_code=403)
    role = await run_in_threadpool(
        control_plane.member_role,
        user["org_id"],
        str(user.get("user_id") or ""),
    )
    if role not in ("owner", "billing"):
        return None, JSONResponse({"error": "forbidden"}, status_code=403)
    return user, None


def _create_checkout(org: str, email: str) -> str:
    sdk = _stripe()
    reservation = control_plane.reserve_checkout(org)
    if not reservation or not reservation.get("ok"):
        raise BillingConflict(
            str((reservation or {}).get("reason") or "checkout_unavailable")
        )
    revision = int(reservation["revision"])
    customer_id = str(reservation.get("customer_id") or "")
    if not customer_id:
        customer = sdk.Customer.create(
            email=email or None,
            metadata={"org_id": org},
            idempotency_key=f"customer:{org}",
        )
        customer_id = str(customer.id)
        if not control_plane.bind_stripe_customer(org, revision, customer_id):
            raise RuntimeError("customer binding rejected")

    expires_at = int(time.time()) + 30 * 60
    session = sdk.checkout.Session.create(
        mode="subscription",
        line_items=[
            {
                "price": settings.stripe_price_solo.strip(),
                "quantity": 1,
            }
        ],
        customer=customer_id,
        success_url=_success_url(),
        cancel_url=_cancel_url(),
        client_reference_id=org,
        metadata={
            "org_id": org,
            "price_id": settings.stripe_price_solo.strip(),
        },
        subscription_data={
            "metadata": {
                "org_id": org,
                "price_id": settings.stripe_price_solo.strip(),
            }
        },
        expires_at=expires_at,
        idempotency_key=f"checkout:{org}:solo:{revision}",
    )
    if not control_plane.finish_checkout(
        org, revision, customer_id, str(session.id), expires_at
    ):
        raise RuntimeError("checkout checkpoint rejected")
    return str(session.url)


@router.post("/billing/checkout")
async def billing_checkout(request: Request) -> JSONResponse:
    if not _billing_ready():
        return _not_enabled()
    user, err = await _billing_principal(request)
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_body"}, status_code=400)
    if (
        not isinstance(body, dict)
        or set(body) != {"plan"}
        or body.get("plan") != "solo"
    ):
        return JSONResponse({"error": "invalid_plan"}, status_code=400)
    try:
        url = await run_in_threadpool(
            _create_checkout,
            user["org_id"],
            str(user.get("email") or ""),
        )
    except BillingConflict as exc:
        return JSONResponse({"error": exc.reason}, status_code=409)
    except Exception:
        return JSONResponse({"error": "checkout_failed"}, status_code=502)
    return JSONResponse({"url": url})


def _create_portal(customer_id: str) -> str:
    sdk = _stripe()
    kwargs = {"customer": customer_id, "return_url": _return_url()}
    configuration = settings.stripe_portal_configuration_id.strip()
    if configuration:
        kwargs["configuration"] = configuration
    return str(sdk.billing_portal.Session.create(**kwargs).url)


@router.post("/billing/portal")
async def billing_portal(request: Request) -> JSONResponse:
    if not _billing_ready():
        return _not_enabled()
    user, err = await _billing_principal(request)
    if err is not None:
        return err
    account = await run_in_threadpool(
        control_plane.get_billing, user["org_id"]
    ) or {}
    customer_id = str(account.get("stripe_customer_id") or "")
    if not customer_id:
        return JSONResponse({"error": "no_customer"}, status_code=400)
    try:
        url = await run_in_threadpool(_create_portal, customer_id)
    except Exception:
        return JSONResponse({"error": "portal_failed"}, status_code=502)
    return JSONResponse({"url": url})


def _construct_event(raw_body: bytes, signature: str):
    return _stripe().Webhook.construct_event(
        raw_body,
        signature,
        settings.stripe_webhook_secret.strip(),
    )


def _customer_id(obj: dict) -> Optional[str]:
    customer = obj.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    return str(customer or "").strip() or None


def _object_id(value) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("id")
    return str(value or "").strip() or None


def _epoch(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _metadata_org(obj: dict) -> Optional[str]:
    value = str((obj.get("metadata") or {}).get("org_id") or "").strip()
    return value or None


def _price_id(value) -> Optional[str]:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return str(value.get("id") or "").strip() or None
    return None


def _subscription_item(obj: dict) -> dict:
    items = (obj.get("items") or {}).get("data") or []
    if not isinstance(items, list) or len(items) != 1:
        return {"valid": False, "start": None, "end": None}
    item = items[0] or {}
    price = _price_id(item.get("price"))
    try:
        quantity = int(item.get("quantity"))
    except (TypeError, ValueError):
        quantity = 0
    period = item.get("period") or {}
    start = _epoch(item.get("current_period_start"))
    end = _epoch(item.get("current_period_end"))
    if start is None:
        start = _epoch(period.get("start"))
    if end is None:
        end = _epoch(period.get("end"))
    return {
        "valid": (
            price == settings.stripe_price_solo.strip() and quantity == 1
        ),
        "start": start,
        "end": end,
    }


def _invoice_subscription(obj: dict) -> Optional[str]:
    parent = obj.get("parent") or {}
    details = parent.get("subscription_details") or {}
    return _object_id(details.get("subscription")) or _object_id(
        obj.get("subscription")
    )


def _invoice_item(obj: dict) -> dict:
    candidates = []
    for line in ((obj.get("lines") or {}).get("data") or []):
        line = line or {}
        parent = line.get("parent") or {}
        item_details = parent.get("subscription_item_details") or {}
        if item_details.get("proration") is True or line.get("proration") is True:
            continue
        pricing = line.get("pricing") or {}
        price_details = pricing.get("price_details") or {}
        price = (
            _price_id(price_details.get("price"))
            or _price_id(line.get("price"))
        )
        try:
            quantity = int(line.get("quantity"))
        except (TypeError, ValueError):
            quantity = 0
        if price != settings.stripe_price_solo.strip() or quantity != 1:
            continue
        period = line.get("period") or {}
        candidates.append(
            {
                "start": _epoch(period.get("start")),
                "end": _epoch(period.get("end")),
            }
        )
    if len(candidates) != 1:
        return {"valid": False, "start": None, "end": None}
    return {"valid": True, **candidates[0]}


def _validated_event(event) -> tuple[str, int, dict]:
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    created = _epoch(event.get("created"))
    livemode = event.get("livemode")
    obj = (event.get("data") or {}).get("object")
    if (
        not _EVENT_ID.fullmatch(event_id)
        or not event_type
        or len(event_type) > 120
        or created is None
        or created <= 0
        or type(livemode) is not bool
        or livemode is not bool(settings.stripe_live_mode)
        or not isinstance(obj, dict)
    ):
        raise InvalidSignedEvent("invalid signed Stripe event")
    return event_type, created, obj


def _compute_effect(event) -> tuple[Optional[str], Optional[dict]]:
    event_type, created, obj = _validated_event(event)
    customer = _customer_id(obj)

    if event_type == "checkout.session.completed":
        metadata = obj.get("metadata") or {}
        if (
            obj.get("mode") != "subscription"
            or str(metadata.get("price_id") or "")
            != settings.stripe_price_solo.strip()
        ):
            return customer, None
        return customer, {
            "kind": "checkout_link",
            "event_created": created,
            "asserted_org": str(
                obj.get("client_reference_id") or metadata.get("org_id") or ""
            ).strip() or None,
            "session_id": _object_id(obj.get("id")),
            "subscription_id": _object_id(obj.get("subscription")),
        }

    if event_type in {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }:
        status = str(obj.get("status") or "").lower()
        if event_type == "customer.subscription.deleted":
            status = "canceled"
        if status in _ACTIVE:
            access = "active"
        elif status == "past_due":
            access = "past_due"
        else:
            access = "terminal"
            if not status:
                status = "invalid"
        item = _subscription_item(obj)
        return customer, {
            "kind": "subscription",
            "event_created": created,
            "asserted_org": _metadata_org(obj),
            "subscription_id": _object_id(obj.get("id")),
            "status": status,
            "access": access,
            "price_valid": bool(item["valid"]),
            "period_start": item["start"],
            "period_end": item["end"],
        }

    if event_type in {"invoice.paid", "invoice.payment_failed"}:
        item = _invoice_item(obj)
        return customer, {
            "kind": (
                "invoice_paid"
                if event_type == "invoice.paid"
                else "invoice_failed"
            ),
            "event_created": created,
            "subscription_id": _invoice_subscription(obj),
            "price_valid": bool(item["valid"]),
            "period_start": item["start"],
            "period_end": item["end"],
        }

    return customer, None


def _bad_signature(exc: Exception) -> bool:
    if isinstance(exc, ValueError):
        return True
    errors = getattr(stripe, "error", None)
    signature_error = getattr(errors, "SignatureVerificationError", None)
    return bool(signature_error and isinstance(exc, signature_error))


def _process_webhook(
    raw_body: bytes, signature: str
) -> tuple[int, dict]:
    try:
        event = _construct_event(raw_body, signature)
    except Exception as exc:
        if _bad_signature(exc):
            return 400, {"error": "invalid_signature"}
        raise
    try:
        event_type, _, _ = _validated_event(event)
        customer, effect = _compute_effect(event)
    except InvalidSignedEvent:
        return 400, {"error": "invalid_event"}
    applied = control_plane.apply_stripe_event(
        str(event.get("id")), event_type, customer, effect
    )
    if applied is None:
        raise RuntimeError("billing database unavailable")
    if applied is False:
        return 200, {"ok": True, "duplicate": True}
    return 200, {"ok": True}


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    if not _billing_ready():
        return _not_enabled()
    limit = max(1024, int(settings.stripe_webhook_max_body_bytes))
    content_length = request.headers.get("content-length", "")
    try:
        if content_length and int(content_length) > limit:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
    except ValueError:
        return JSONResponse({"error": "invalid_content_length"}, status_code=400)
    raw_body = await request.body()
    if len(raw_body) > limit:
        return JSONResponse({"error": "payload_too_large"}, status_code=413)
    signature = request.headers.get("stripe-signature", "")
    if not signature:
        return JSONResponse({"error": "invalid_signature"}, status_code=400)
    try:
        status, body = await run_in_threadpool(
            _process_webhook, raw_body, signature
        )
    except Exception:
        return JSONResponse({"error": "webhook_retry"}, status_code=503)
    return JSONResponse(body, status_code=status)

