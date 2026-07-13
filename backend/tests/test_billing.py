"""Adversarial key-free tests for Stripe event parsing and HTTP guards."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import billing
from app.config import settings


@pytest.fixture
def stripe_config(monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_example")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_example")
    monkeypatch.setattr(settings, "stripe_price_solo", "price_solo")
    monkeypatch.setattr(settings, "stripe_live_mode", False)
    monkeypatch.setattr(settings, "stripe_api_version", "2026-02-25.clover")
    monkeypatch.setattr(settings, "public_base_url", "https://app.lauravatar.com")
    monkeypatch.setattr(billing, "stripe", object())
    monkeypatch.setattr(billing.control_plane, "enabled", lambda: True)
    monkeypatch.setattr(
        billing.control_plane, "billing_boundary_ready", lambda: True
    )


def _event(event_type, obj, *, created=100, event_id="evt_test", live=False):
    return {
        "id": event_id,
        "type": event_type,
        "created": created,
        "livemode": live,
        "data": {"object": obj},
    }


def _subscription(
    *,
    sub="sub_1",
    customer="cus_1",
    status="active",
    price="price_solo",
    quantity=1,
    start=1000,
    end=2000,
):
    return {
        "id": sub,
        "customer": customer,
        "status": status,
        "metadata": {"org_id": "org-a"},
        "items": {
            "data": [
                {
                    "id": "si_1",
                    "price": {"id": price},
                    "quantity": quantity,
                    "current_period_start": start,
                    "current_period_end": end,
                }
            ]
        },
    }


def test_readiness_is_full_boundary_not_toggle(stripe_config, monkeypatch):
    assert billing._billing_ready() is True
    monkeypatch.setattr(settings, "stripe_secret_key", "rk_test_restricted")
    assert billing._billing_ready() is True
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_example")
    monkeypatch.setattr(settings, "public_base_url", "http://app.lauravatar.com")
    assert billing._billing_ready() is False
    monkeypatch.setattr(settings, "public_base_url", "https://evil.example")
    assert billing._billing_ready() is False
    monkeypatch.setattr(settings, "public_base_url", "https://app.lauravatar.com")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_live_wrong-mode")
    assert billing._billing_ready() is False
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_example")
    monkeypatch.setattr(
        billing.control_plane, "billing_boundary_ready", lambda: False
    )
    assert billing._billing_ready() is False


def test_checkout_completion_is_linkage_only(stripe_config):
    customer, effect = billing._compute_effect(
        _event(
            "checkout.session.completed",
            {
                "id": "cs_1",
                "mode": "subscription",
                "customer": "cus_1",
                "subscription": "sub_1",
                "client_reference_id": "org-a",
                "metadata": {
                    "org_id": "org-a",
                    "price_id": "price_solo",
                },
            },
        )
    )
    assert customer == "cus_1"
    assert effect["kind"] == "checkout_link"
    assert "plan" not in effect
    assert "included_seconds" not in effect


def test_subscription_uses_current_clover_item_period(stripe_config):
    customer, effect = billing._compute_effect(
        _event(
            "customer.subscription.updated",
            _subscription(start=1700, end=2700),
        )
    )
    assert customer == "cus_1"
    assert effect == {
        "kind": "subscription",
        "event_created": 100,
        "asserted_org": "org-a",
        "subscription_id": "sub_1",
        "status": "active",
        "access": "active",
        "price_valid": True,
        "period_start": 1700,
        "period_end": 2700,
    }


@pytest.mark.parametrize("status", ["paused", "incomplete", "unpaid", "mystery"])
def test_unsafe_subscription_statuses_fail_closed(stripe_config, status):
    _, effect = billing._compute_effect(
        _event(
            "customer.subscription.updated",
            _subscription(status=status),
        )
    )
    assert effect["access"] == "terminal"


def test_wrong_price_or_quantity_never_provisions(stripe_config):
    _, wrong_price = billing._compute_effect(
        _event(
            "customer.subscription.created",
            _subscription(price="price_attacker"),
        )
    )
    _, wrong_quantity = billing._compute_effect(
        _event(
            "customer.subscription.created",
            _subscription(quantity=2),
        )
    )
    assert wrong_price["price_valid"] is False
    assert wrong_quantity["price_valid"] is False


def test_invoice_selects_exact_non_proration_clover_line(stripe_config):
    obj = {
        "id": "in_1",
        "customer": "cus_1",
        "parent": {
            "subscription_details": {"subscription": "sub_1"}
        },
        "lines": {
            "data": [
                {
                    "quantity": 1,
                    "pricing": {
                        "price_details": {"price": "price_other"}
                    },
                    "period": {"start": 1, "end": 2},
                    "parent": {
                        "subscription_item_details": {"proration": False}
                    },
                },
                {
                    "quantity": 1,
                    "pricing": {
                        "price_details": {"price": "price_solo"}
                    },
                    "period": {"start": 3000, "end": 4000},
                    "parent": {
                        "subscription_item_details": {"proration": False}
                    },
                },
            ]
        },
    }
    customer, effect = billing._compute_effect(_event("invoice.paid", obj))
    assert customer == "cus_1"
    assert effect["subscription_id"] == "sub_1"
    assert effect["price_valid"] is True
    assert effect["period_start"] == 3000
    assert effect["period_end"] == 4000


def test_proration_only_invoice_is_not_valid_solo_cycle(stripe_config):
    obj = {
        "id": "in_1",
        "customer": "cus_1",
        "parent": {
            "subscription_details": {"subscription": "sub_1"}
        },
        "lines": {
            "data": [
                {
                    "quantity": 1,
                    "pricing": {
                        "price_details": {"price": "price_solo"}
                    },
                    "period": {"start": 3000, "end": 4000},
                    "parent": {
                        "subscription_item_details": {"proration": True}
                    },
                }
            ]
        },
    }
    _, effect = billing._compute_effect(_event("invoice.paid", obj))
    assert effect["price_valid"] is False


def test_signed_event_mode_and_shape_are_validated(stripe_config):
    with pytest.raises(billing.InvalidSignedEvent):
        billing._compute_effect(
            _event(
                "customer.subscription.created",
                _subscription(),
                live=True,
            )
        )
    with pytest.raises(billing.InvalidSignedEvent):
        billing._compute_effect(
            _event(
                "customer.subscription.created",
                _subscription(),
                event_id="not-an-event",
            )
        )


def test_bad_signature_is_400_but_internal_failure_is_retryable(
    stripe_config, monkeypatch
):
    class SignatureError(Exception):
        pass

    class Errors:
        SignatureVerificationError = SignatureError

    class FakeStripe:
        error = Errors

    monkeypatch.setattr(billing, "stripe", FakeStripe())
    monkeypatch.setattr(
        billing,
        "_construct_event",
        lambda *_: (_ for _ in ()).throw(SignatureError()),
    )
    assert billing._process_webhook(b"{}", "sig")[0] == 400

    app = FastAPI()
    app.include_router(billing.router)
    monkeypatch.setattr(billing, "_billing_ready", lambda: True)
    monkeypatch.setattr(
        billing,
        "_process_webhook",
        lambda *_: (_ for _ in ()).throw(RuntimeError("database down")),
    )
    client = TestClient(app)
    response = client.post(
        "/webhooks/stripe",
        content=b"{}",
        headers={"stripe-signature": "sig"},
    )
    assert response.status_code == 503
    assert response.json() == {"error": "webhook_retry"}


def test_checkout_requires_exact_origin(stripe_config, monkeypatch):
    app = FastAPI()
    app.include_router(billing.router)
    monkeypatch.setattr(billing, "_billing_ready", lambda: True)
    monkeypatch.setattr(
        billing.auth,
        "current_user",
        lambda request: {
            "org_id": "org-a",
            "user_id": "00000000-0000-0000-0000-000000000001",
            "email": "owner@example.test",
        },
    )
    monkeypatch.setattr(billing.control_plane, "member_role", lambda *_: "owner")
    monkeypatch.setattr(
        billing, "_create_checkout", lambda *_: "https://checkout.stripe.test/cs"
    )
    client = TestClient(app)
    denied = client.post(
        "/billing/checkout",
        json={"plan": "solo"},
        headers={"origin": "https://evil.example"},
    )
    assert denied.status_code == 403
    allowed = client.post(
        "/billing/checkout",
        json={"plan": "solo"},
        headers={"origin": "https://app.lauravatar.com"},
    )
    assert allowed.status_code == 200


def test_webhook_body_limit_before_processing(stripe_config, monkeypatch):
    app = FastAPI()
    app.include_router(billing.router)
    monkeypatch.setattr(billing, "_billing_ready", lambda: True)
    monkeypatch.setattr(settings, "stripe_webhook_max_body_bytes", 1024)
    client = TestClient(app)
    response = client.post(
        "/webhooks/stripe",
        content=b"x" * 1025,
        headers={"stripe-signature": "sig"},
    )
    assert response.status_code == 413


def test_checkout_uses_stable_customer_and_revisioned_session_keys(
    stripe_config, monkeypatch
):
    calls = {}

    class Item:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Customer:
        @staticmethod
        def create(**kwargs):
            calls["customer"] = kwargs
            return Item(id="cus_1")

    class Session:
        @staticmethod
        def create(**kwargs):
            calls["session"] = kwargs
            return Item(
                id="cs_1",
                url="https://checkout.stripe.test/cs_1",
            )

    class Checkout:
        pass

    Checkout.Session = Session

    class SDK:
        pass

    SDK.Customer = Customer
    SDK.checkout = Checkout

    monkeypatch.setattr(billing, "_stripe", lambda: SDK)
    monkeypatch.setattr(
        billing.control_plane,
        "reserve_checkout",
        lambda org: {"ok": True, "revision": 7, "customer_id": None},
    )
    monkeypatch.setattr(
        billing.control_plane, "bind_stripe_customer", lambda *_: True
    )
    monkeypatch.setattr(
        billing.control_plane, "finish_checkout", lambda *_: True
    )
    url = billing._create_checkout("org-a", "owner@example.test")
    assert url.endswith("/cs_1")
    assert calls["customer"]["idempotency_key"] == "customer:org-a"
    assert calls["session"]["idempotency_key"] == "checkout:org-a:solo:7"
    assert calls["session"]["line_items"] == [
        {"price": "price_solo", "quantity": 1}
    ]


def test_checkout_releases_only_before_session_call(stripe_config, monkeypatch):
    released = []

    class CustomerFails:
        @staticmethod
        def create(**_kwargs):
            raise RuntimeError("customer failed")

    class SDKCustomerFails:
        pass

    SDKCustomerFails.Customer = CustomerFails

    monkeypatch.setattr(billing, "_stripe", lambda: SDKCustomerFails)
    monkeypatch.setattr(
        billing.control_plane,
        "reserve_checkout",
        lambda _org: {"ok": True, "revision": 9, "customer_id": None},
    )
    monkeypatch.setattr(
        billing.control_plane,
        "release_checkout",
        lambda org, revision: released.append((org, revision)),
    )
    with pytest.raises(RuntimeError):
        billing._create_checkout("org-a", "owner@example.test")
    assert released == [("org-a", 9)]

    class SessionFails:
        @staticmethod
        def create(**_kwargs):
            raise RuntimeError("ambiguous session failure")

    class Checkout:
        pass

    Checkout.Session = SessionFails

    class SDKSessionFails:
        pass

    SDKSessionFails.checkout = Checkout
    monkeypatch.setattr(billing, "_stripe", lambda: SDKSessionFails)
    monkeypatch.setattr(
        billing.control_plane,
        "reserve_checkout",
        lambda _org: {
            "ok": True, "revision": 10, "customer_id": "cus_existing"
        },
    )
    with pytest.raises(RuntimeError):
        billing._create_checkout("org-a", "owner@example.test")
    assert released == [("org-a", 9)]
