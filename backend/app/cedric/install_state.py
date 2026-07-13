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
    """Verify + decode a state we minted in ``pack``. None on ANY failure —
    bad shape, wrong signature, expired, or wrong version — so a forged or
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
    except Exception:  # noqa: BLE001 — hostile payloads must not raise
        return None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None
    if float(data.get("exp") or 0) < (now if now is not None else time.time()):
        return None
    return data


def install_url(
    org_id: str,
    avatar_id: str,
    channel: str = "",
    return_url: str = "",
    complete_url: str = "",
) -> str:
    orgs_url = settings.cedric_orgs_url.strip()
    if not orgs_url:
        raise RuntimeError("brain provisioning is not configured")
    base = orgs_url.rstrip("/").rsplit("/api/laura/orgs", 1)[0]
    state = pack(org_id, avatar_id, channel, return_url, complete_url)
    return f"{base}/api/slack/install?{urlencode({'state': state})}"
