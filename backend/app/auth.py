"""Dashboard login — Google Sign-In + a signed session cookie.

The identity layer for the owner dashboard: a user signs in with Google
(OIDC authorization-code flow), gets a row in the users table and an
HttpOnly HMAC-signed cookie, and from then on the dashboard scopes what it
shows to their org (org_id == user_id today — the multi-tenancy seam of
docs/infra/MULTI-TENANCY.md, not the full build).

Deliberate properties:
- Key-free demo preserved: with no Google client configured, ``enabled()``
  is False and every gate in dashboard.py falls back to today's behavior
  (open, or bearer-token when LAURA_API_TOKEN is set).
- Reuses the SAME Google OAuth client as calendar auto-join — one more
  redirect URI ({public_base_url}/auth/google/callback) in the console,
  no new secrets.
- The id_token is accepted from Google's token endpoint DIRECTLY over TLS
  (not from the browser), so we validate iss/aud/exp/email_verified but
  skip JWKS signature verification — the token can't have been tampered
  with in transit. If we ever accept tokens from the client, add JWKS.
- Cookie = base64(payload).hmac_sha256(secret). No JWT dependency; stdlib
  only. SESSION_SECRET empty -> random per boot (re-login after restart).
- Machine callers are untouched: Cedric keeps using the bearer token; the
  cookie is for humans in a browser.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.background import BackgroundTask

from . import store
from .config import settings

router = APIRouter(tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

COOKIE_NAME = "laura_session"
STATE_COOKIE = "laura_oauth_state"  # binds the OAuth flow to THIS browser
SESSION_TTL_SECONDS = 14 * 24 * 3600  # two weeks
STATE_TTL_SECONDS = 600  # ten minutes to complete the Google round-trip

# Per-boot fallback signing key (see config.session_secret). Module-level so
# every worker thread signs/verifies consistently within one process.
_BOOT_SECRET = secrets.token_hex(32)


def _secret() -> bytes:
    return (settings.session_secret.strip() or _BOOT_SECRET).encode()


def enabled() -> bool:
    """Login is available when the Google OAuth client is configured."""
    return bool(
        settings.google_calendar_client_id
        and settings.google_calendar_client_secret
    )


def _redirect_uri() -> str:
    return f"{settings.public_base_url.rstrip('/')}/auth/google/callback"


# ── cookie signing ─────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, purpose: str) -> str:
    # `purpose` domain-separates the two things this key signs (session cookie
    # vs OAuth state) so a token minted for one can never be replayed as the
    # other. Signing over bytes; the digest is ascii hex.
    msg = f"{purpose}:{payload}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def _verify(payload: str, signature: str, purpose: str) -> bool:
    """Constant-time check that is robust to a hostile signature — a non-ASCII
    byte in an attacker-supplied cookie must not raise (compare_digest on str
    with non-ASCII does), so we compare encoded bytes and swallow anything."""
    try:
        return hmac.compare_digest(
            signature.encode("utf-8", "ignore"), _sign(payload, purpose).encode()
        )
    except Exception:
        return False


def make_cookie(user_id: str, ttl: int = SESSION_TTL_SECONDS) -> str:
    payload = _b64(json.dumps({"uid": user_id, "exp": time.time() + ttl}).encode())
    return f"{payload}.{_sign(payload, 'session')}"


def read_cookie(value: str) -> Optional[str]:
    """user_id from a valid, unexpired cookie value, else None. Never raises."""
    if not value or "." not in value:
        return None
    try:
        payload, signature = value.rsplit(".", 1)
        if not _verify(payload, signature, "session"):
            return None
        data = json.loads(_unb64(payload))
        if float(data.get("exp", 0)) < time.time():
            return None
        return str(data.get("uid") or "") or None
    except Exception:
        return None


def current_user(request: Request) -> Optional[dict]:
    """The logged-in user for this request, or None. A cookie whose user row
    vanished (ephemeral store wiped by a redeploy) is treated as logged-out —
    the user just signs in again and lands on the same user_id (it derives
    from the email)."""
    uid = read_cookie(request.cookies.get(COOKIE_NAME, ""))
    return store.get_user(uid) if uid else None


def email_allowed(email: str) -> bool:
    """Whether this Google email may sign in. Empty allowlist = allow all
    (Google's testing-mode test-user list is the gate in that case)."""
    allow = settings.dashboard_allowed_emails.strip()
    if not allow:
        return True
    email = (email or "").strip().lower()
    for entry in (e.strip().lower() for e in allow.split(",")):
        if not entry:
            continue
        if entry.startswith("@") and email.endswith(entry):
            return True
        if entry == email:
            return True
    return False


def _login_required() -> JSONResponse:
    return JSONResponse(
        {"error": "login_required", "auth_enabled": True}, status_code=401
    )


def gate(request: Request) -> Optional[JSONResponse]:
    """Access gate shared by the endpoints that used to call cedric.auth_error
    directly (/dashboard/summary, /sessions/start, /sessions/{id}/end). Returns
    an error response to send, or None to allow. Truth table for a request that
    is NOT a logged-in cookie user:

        token set + valid bearer     -> allow (machine caller: Cedric)
        token set + bad/no bearer:
            login enabled            -> 401 login_required (browser -> Google)
            login disabled           -> 401 unauthorized  (browser -> token box)
        no token + login enabled     -> 401 login_required (browser -> Google)
        no token + login disabled    -> allow (key-free demo, unchanged)
    """
    from . import cedric  # local import: cedric never imports auth (no cycle)

    if current_user(request) is not None:
        return None  # logged-in humans are handled (and org-scoped) by callers
    token_set = bool(settings.laura_api_token.strip())
    bearer_err = cedric.auth_error(request)  # None = valid bearer OR no token
    if token_set:
        if bearer_err is None:
            return None  # valid machine bearer
        return _login_required() if enabled() else bearer_err
    return _login_required() if enabled() else None


# ── routes ─────────────────────────────────────────────────────────────

@router.get("/auth/google/start")
def google_start() -> RedirectResponse:
    if not enabled():
        return RedirectResponse("/login?error=not_configured", status_code=302)
    # Bind this flow to THIS browser: a random nonce lives in an HttpOnly cookie
    # AND (signed) inside the state param. The callback requires both to match,
    # so an attacker's pre-obtained signed state can't be planted in a victim's
    # browser (login CSRF / session fixation). The signed state stays single-use
    # because the cookie is cleared on the first successful callback.
    nonce = secrets.token_hex(16)
    payload = _b64(
        json.dumps({"n": nonce, "exp": time.time() + STATE_TTL_SECONDS}).encode()
    )
    params = {
        "client_id": settings.google_calendar_client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": f"{payload}.{_sign(payload, 'state')}",
        "prompt": "select_account",
    }
    resp = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)
    resp.set_cookie(
        STATE_COOKIE,
        nonce,
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.public_base_url.startswith("https"),
        path="/auth",
    )
    return resp


def _err_redirect(reason: str) -> RedirectResponse:
    resp = RedirectResponse(f"/login?error={reason}", status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/auth")
    return resp


@router.get("/auth/google/callback")
async def google_callback(request: Request) -> RedirectResponse:
    if not enabled():
        return RedirectResponse("/login?error=not_configured", status_code=302)

    # CSRF: the state must be one we signed, fresh, AND its nonce must match the
    # cookie set on THIS browser at /start.
    state = request.query_params.get("state", "")
    payload = state.rsplit(".", 1)[0] if "." in state else ""
    if not payload or not _verify(payload, state.rsplit(".", 1)[-1], "state"):
        return _err_redirect("state")
    try:
        state_data = json.loads(_unb64(payload))
    except Exception:
        return _err_redirect("state")
    if float(state_data.get("exp", 0)) < time.time():
        return _err_redirect("expired")
    cookie_nonce = request.cookies.get(STATE_COOKIE, "")
    if not cookie_nonce or not hmac.compare_digest(
        cookie_nonce.encode("utf-8", "ignore"),
        str(state_data.get("n", "")).encode(),
    ):
        return _err_redirect("state")

    code = request.query_params.get("code", "")
    if not code:
        return _err_redirect("denied")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.google_calendar_client_id,
                    "client_secret": settings.google_calendar_client_secret,
                    "redirect_uri": _redirect_uri(),
                    "grant_type": "authorization_code",
                },
            )
    except Exception:
        return _err_redirect("exchange")
    if token_resp.status_code != 200:
        return _err_redirect("exchange")

    claims = _decode_id_token(token_resp.json().get("id_token", ""))
    if claims is None:
        return _err_redirect("token")
    if not email_allowed(claims["email"]):
        return _err_redirect("not_allowed")

    # Threadpooled: upsert_user is sync SQLite and — when the control plane is
    # configured — a sync Postgres round-trip (ensure_user). This handler is
    # async, so running it inline would block the shared event loop that also
    # serves every live meeting (single instance).
    user = await run_in_threadpool(
        store.upsert_user,
        email=claims["email"],
        name=str(claims.get("name") or ""),
        picture=str(claims.get("picture") or ""),
        # The Google OIDC subject: the durable control plane (when configured)
        # keys the user on it, so an email change never forks the identity.
        google_sub=str(claims.get("sub") or ""),
    )
    background = None
    if settings.cedric_orgs_url.strip():
        # Run after the redirect is sent, but attach it to the response instead
        # of spawning an untracked task. The idempotent pending endpoint is
        # retried on every login, so a transient first-signup failure heals
        # without an ops step and Add to Slack can fill team_id later.
        from . import cedric

        background = BackgroundTask(
            cedric.provision_org, user["org_id"], None, "", "cedric"
        )
    response = RedirectResponse(
        "/dashboard", status_code=302, background=background
    )
    response.set_cookie(
        COOKIE_NAME,
        make_cookie(user["user_id"]),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.public_base_url.startswith("https"),
        path="/",
    )
    response.delete_cookie(STATE_COOKIE, path="/auth")  # single-use
    return response


def _decode_id_token(id_token: str) -> Optional[dict]:
    """Claims from Google's id_token, validated for iss/aud/exp/email.
    Signature is NOT checked — see the module docstring for why that is
    sound here (token comes straight from Google's token endpoint over TLS).
    """
    try:
        body = id_token.split(".")[1]
        claims = json.loads(_unb64(body))
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        return None
    if claims.get("aud") != settings.google_calendar_client_id:
        return None
    if float(claims.get("exp", 0)) < time.time():
        return None
    email = str(claims.get("email") or "").strip().lower()
    if not email or not claims.get("email_verified", False):
        return None
    claims["email"] = email
    return claims


def _first_party_origin(origin: str) -> bool:
    """True if ``origin`` is one of Laura's OWN browser-facing origins.

    ``public_base_url`` is the App Runner URL (it backs the OAuth redirect_uri and
    internal links), but real users reach the dashboard at **lauravatar.com**
    through the Cloudflare Worker — so their genuinely same-origin logout POST
    carries ``Origin: https://lauravatar.com``, which is first-party, NOT
    cross-site. Comparing only against ``public_base_url`` 403'd every real logout
    (and the two dashboard POSTs that share this check). The host allowlist mirrors
    billing._public_origin so the app's CSRF checks agree on what is first-party."""
    o = (origin or "").rstrip("/")
    if not o:
        return False
    if o == settings.public_base_url.rstrip("/"):
        return True
    try:
        parsed = urlsplit(o)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return (
        host == "lauravatar.com"
        or host.endswith(".lauravatar.com")
        or host.endswith(".awsapprunner.com")
    )


def _same_origin(request: Request) -> bool:
    """Reject a cross-site POST (forced-logout CSRF). SameSite=Lax already keeps
    the cookie off cross-site POSTs, but this is cheap defense-in-depth: allow
    only a first-party origin (see _first_party_origin) or a fetch-metadata
    same-origin request."""
    origin = request.headers.get("origin", "")
    if origin:
        return _first_party_origin(origin)
    # No Origin header (older browsers / same-origin navigations): fall back to
    # the fetch-metadata site signal when present.
    site = request.headers.get("sec-fetch-site", "")
    return site in ("", "same-origin", "same-site", "none")


@router.post("/auth/logout")
def logout(request: Request) -> RedirectResponse:
    if not _same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/auth/me")
def me(request: Request) -> JSONResponse:
    user = current_user(request)
    if user is None:
        return JSONResponse(
            {"user": None, "auth_enabled": enabled()}, status_code=401
        )
    return JSONResponse({"user": user, "auth_enabled": enabled()})


@router.get("/auth/allowed")
def allowed(email: str = "") -> JSONResponse:
    """Private-beta gate check for the login page. `gated` is True when an
    allowlist is configured at all (DASHBOARD_ALLOWED_EMAILS) — the page skips
    the email step and goes straight to Google when it isn't. `allowed` says
    whether THIS email may proceed to Google; a non-allowed email is shown a
    'coming soon' waitlist message instead of Google's unverified-app wall.
    Reveals only a boolean, never the list."""
    return JSONResponse(
        {
            "allowed": email_allowed(email),
            "gated": bool(settings.dashboard_allowed_emails.strip()),
            "auth_enabled": enabled(),
        }
    )
