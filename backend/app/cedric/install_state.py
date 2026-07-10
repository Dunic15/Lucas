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
    # Production uses the same deployment credential Cedric accepts on
    # /api/laura/orgs. CEDRIC_ORGS_TOKEN remains an optional override.
    key = settings.cedric_orgs_token.strip() or settings.laura_api_token.strip()
    if not key:
        raise RuntimeError("brain provisioning is not configured")
    return hmac.new(key.encode(), _PURPOSE + payload.encode(), hashlib.sha256).hexdigest()


def pack(
    org_id: str,
    avatar_id: str,
    channel: str,
    return_url: str,
    *,
    now: float | None = None,
) -> str:
    data = {
        "v": 1,
        "org_id": org_id,
        "avatar_id": avatar_id,
        "channel": channel,
        "return_url": return_url,
        "exp": int(now if now is not None else time.time()) + _TTL_SECONDS,
        "nonce": secrets.token_hex(16),
    }
    payload = _b64(json.dumps(data, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload)}"


def install_url(
    org_id: str, avatar_id: str, channel: str = "", return_url: str = ""
) -> str:
    orgs_url = settings.cedric_orgs_url.strip()
    if not orgs_url:
        raise RuntimeError("brain provisioning is not configured")
    base = orgs_url.rstrip("/").rsplit("/api/laura/orgs", 1)[0]
    state = pack(org_id, avatar_id, channel, return_url)
    return f"{base}/api/slack/install?{urlencode({'state': state})}"
