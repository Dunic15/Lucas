"""Signed handoff from Laura's dashboard to Cedric's Slack OAuth flow."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

from ..config import settings

_PURPOSE = b"laura-slack-install:"
_ORG_TOKEN_PURPOSE = b"laura-org-token:"
_TTL_SECONDS = 600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _sign(payload: str) -> str:
    # Dedicated bootstrap credential shared only by Laura's signed handoff
    # and Cedric's provisioning callback. The general session bearer is never
    # a cross-tenant provisioning key.
    key = settings.cedric_orgs_token.strip()
    if not key:
        raise RuntimeError("brain provisioning is not configured")
    return hmac.new(key.encode(), _PURPOSE + payload.encode(), hashlib.sha256).hexdigest()


def derive_org_token(org_id: str, nonce: str) -> str:
    """Deterministic, retry-safe Cedric→Laura bearer for one verified install.

    The raw token is returned only on the authenticated server-to-server
    completion response. Laura persists SHA-256(raw) only. The purpose label is
    deliberately distinct from Slack-state signing, and a new install nonce
    rotates the credential while a lost-response retry derives the same value.
    """
    org = (org_id or "").strip()
    install_nonce = (nonce or "").strip()
    key = settings.cedric_orgs_token.strip()
    if not key or not org or not install_nonce:
        raise RuntimeError("brain provisioning is not configured")
    material = f"{org}:{install_nonce}".encode()
    digest = hmac.new(key.encode(), _ORG_TOKEN_PURPOSE + material, hashlib.sha256).digest()
    return "laura_org_" + _b64(digest)


def pack(
    org_id: str,
    avatar_id: str,
    channel: str,
    return_url: str,
    complete_url: str = "",
    *,
    now: float | None = None,
) -> str:
    data = {
        "v": 1,
        "org_id": org_id,
        "avatar_id": avatar_id,
        "channel": channel,
        "return_url": return_url,
        "complete_url": complete_url,
        "exp": int(now if now is not None else time.time()) + _TTL_SECONDS,
        "nonce": secrets.token_hex(16),
    }
    payload = _b64(json.dumps(data, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload)}"


def unpack(state: str, *, now: float | None = None) -> dict | None:
    """Verify + decode a state we minted in ``pack``. None on ANY failure -
    bad shape, wrong signature, expired, or wrong version; so a forged or
    stale state can never bind an install (the caller treats None as
    'not initiated'). Constant-time signature compare; never raises on
    hostile input (a missing signing key is 'cannot verify' → None)."""
    raw = (state or "").strip()
    if "." not in raw:
        return None
    payload, _, signature = raw.rpartition(".")
    try:
        expected = _sign(payload)
    except RuntimeError:
        return None  # provisioning unconfigured: nothing we minted can verify
    if not hmac.compare_digest(signature.encode("utf-8", "ignore"), expected.encode()):
        return None
    try:
        pad = "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload + pad))
    except Exception:  # noqa: BLE001; hostile payloads must not raise
        return None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None
    if float(data.get("exp") or 0) < (now if now is not None else time.time()):
        return None
    return data


def install_url_and_state(
    org_id: str,
    avatar_id: str,
    channel: str = "",
    return_url: str = "",
    complete_url: str = "",
) -> tuple[str, str]:
    """Build Cedric's stable install URL and return its opaque state for the
    server-side pending-nonce checkpoint. The raw state goes only in the
    browser redirect; no org bearer is embedded in it."""
    orgs_url = settings.cedric_orgs_url.strip()
    if not orgs_url:
        raise RuntimeError("brain provisioning is not configured")
    base = orgs_url.rstrip("/").rsplit("/api/laura/orgs", 1)[0]
    state = pack(org_id, avatar_id, channel, return_url, complete_url)
    return f"{base}/api/slack/install?{urlencode({'state': state})}", state


def install_url(
    org_id: str,
    avatar_id: str,
    channel: str = "",
    return_url: str = "",
    complete_url: str = "",
) -> str:
    return install_url_and_state(
        org_id, avatar_id, channel, return_url, complete_url
    )[0]
