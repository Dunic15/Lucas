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
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import store
from .config import settings

router = APIRouter(tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

COOKIE_NAME = "laura_session"
SESSION_TTL_SECONDS = 14 * 24 * 3600  # two weeks

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


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_cookie(user_id: str, ttl: int = SESSION_TTL_SECONDS) -> str:
    payload = _b64(json.dumps({"uid": user_id, "exp": time.time() + ttl}).encode())
    return f"{payload}.{_sign(payload)}"


def read_cookie(value: str) -> Optional[str]:
    """user_id from a valid, unexpired cookie value, else None."""
    if not value or "." not in value:
        return None
    payload, signature = value.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if float(data.get("exp", 0)) < time.time():
        return None
    return str(data.get("uid") or "") or None


def current_user(request: Request) -> Optional[dict]:
    """The logged-in user for this request, or None. A cookie whose user row
    vanished (ephemeral store wiped by a redeploy) is treated as logged-out —
    the user just signs in again and lands on the same user_id (it derives
    from the email)."""
    uid = read_cookie(request.cookies.get(COOKIE_NAME, ""))
    return store.get_user(uid) if uid else None


# ── routes ─────────────────────────────────────────────────────────────

@router.get("/auth/google/start")
def google_start() -> RedirectResponse:
    if not enabled():
        return RedirectResponse("/login?error=not_configured", status_code=302)
    state = _b64(json.dumps({"n": secrets.token_hex(8), "exp": time.time() + 600}).encode())
    params = {
        "client_id": settings.google_calendar_client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": f"{state}.{_sign(state)}",
        "prompt": "select_account",
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)


@router.get("/auth/google/callback")
async def google_callback(request: Request) -> RedirectResponse:
    if not enabled():
        return RedirectResponse("/login?error=not_configured", status_code=302)

    # CSRF: the state must be one we signed, and fresh.
    state = request.query_params.get("state", "")
    payload = state.rsplit(".", 1)[0] if "." in state else ""
    if not payload or not hmac.compare_digest(
        state, f"{payload}.{_sign(payload)}"
    ):
        return RedirectResponse("/login?error=state", status_code=302)
    try:
        state_data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return RedirectResponse("/login?error=state", status_code=302)
    if float(state_data.get("exp", 0)) < time.time():
        return RedirectResponse("/login?error=expired", status_code=302)

    code = request.query_params.get("code", "")
    if not code:
        return RedirectResponse("/login?error=denied", status_code=302)

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
    if token_resp.status_code != 200:
        return RedirectResponse("/login?error=exchange", status_code=302)

    id_token = token_resp.json().get("id_token", "")
    claims = _decode_id_token(id_token)
    if claims is None:
        return RedirectResponse("/login?error=token", status_code=302)

    user = store.upsert_user(
        email=claims["email"],
        name=str(claims.get("name") or ""),
        picture=str(claims.get("picture") or ""),
    )
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(
        COOKIE_NAME,
        make_cookie(user["user_id"]),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.public_base_url.startswith("https"),
        path="/",
    )
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


@router.post("/auth/logout")
def logout() -> RedirectResponse:
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
