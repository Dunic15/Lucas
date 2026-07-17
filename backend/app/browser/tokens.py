"""Opaque presentation tokens (B0) — read-only live-view grants.

A watcher never receives a permanent provider URL or credential. ``present``
mints an opaque token; the frontend exchanges it server-side for the CURRENT
viewer payload. The token is:

- short-lived (``BROWSER_PRESENTATION_TOKEN_TTL_SECONDS``),
- scoped to ONE org + ONE browser session,
- read-only,
- revocable (and auto-revoked when the session closes/expires/revokes),
- replay-checked (expiry + revoke + session-liveness on every exchange),
- exchanged SERVER-SIDE (the token value never appears in a DB row or log —
  only its sha256 hash is stored).

The random value is returned to the caller exactly once at mint time.
"""
from __future__ import annotations

import hashlib
import secrets

_PREFIX = "lbt_"  # laura browser token


def mint() -> tuple[str, str]:
    """(token_value, token_hash). The value is shown once; only the hash is
    persisted."""
    value = _PREFIX + secrets.token_urlsafe(32)
    return value, hash_token(value)


def hash_token(value: str) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()


def looks_like_token(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)
