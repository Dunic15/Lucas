"""Authenticated symmetric encryption for secrets at rest.

Protects the per-org Google refresh token (``store.py`` org_oauth) so a leak of
the SQLite file never exposes a live OAuth credential; see
docs/product/NATIVE-INTEGRATIONS-PLAN.md ("persist encrypted, never in git").

Cipher: **Fernet** (AES-128-CBC + HMAC-SHA256) from the vetted ``cryptography``
library; deliberately NOT a hand-rolled construction, so it survives a security
review. The 32-byte Fernet key is derived from the caller's secret via SHA-256,
so call sites keep passing an arbitrary key string and can source it from AWS SSM
SecureString / KMS (see ``store._oauth_enc_secret``) without changing.

Go-live: the secret must come from ``GOOGLE_TOKEN_ENC_KEY``: a random value held
in SSM SecureString (KMS-encrypted at rest, as the Cedric bearer is), rotated
independently of the session cookie key.

Backward compatibility: :func:`decrypt` still reads tokens written by the earlier
dependency-free HMAC-CTR scheme (:func:`_legacy_decrypt`), so upgrading in place
never invalidates already-stored tokens. New tokens are always Fernet.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct

from cryptography.fernet import Fernet

_PURPOSE = b"laura-secret-v1:"


def _fernet(secret: str) -> Fernet:
    """A Fernet cipher whose key is derived (SHA-256) from the caller secret."""
    key = base64.urlsafe_b64encode(
        hashlib.sha256(_PURPOSE + (secret or "").encode()).digest()
    )
    return Fernet(key)


def encrypt(plaintext: str, secret: str) -> str:
    """Encrypt ``plaintext`` under ``secret``; returns an opaque Fernet token."""
    return _fernet(secret).encrypt((plaintext or "").encode()).decode()


def decrypt(token: str, secret: str) -> str:
    """Inverse of :func:`encrypt`. Raises ``ValueError`` on a bad/forged/foreign
    token (wrong key, truncation, tampering); callers treat that as "no token".
    Falls back to the legacy HMAC-CTR reader so tokens written before the Fernet
    upgrade still decrypt."""
    try:
        return _fernet(secret).decrypt((token or "").encode()).decode()
    except Exception:
        pass  # not a Fernet token (or wrong key); try the legacy reader below
    try:
        return _legacy_decrypt(token, secret)
    except ValueError:
        raise
    except Exception as exc:  # malformed base64, bad utf-8, etc.
        raise ValueError("authentication failed") from exc


# ── legacy HMAC-CTR reader (pre-Fernet tokens) ──────────────────────────────
# Kept ONLY so an in-place upgrade can still read tokens written by the previous
# dependency-free scheme. New tokens are always Fernet (see :func:`encrypt`).
# Legacy format (base64url, one string): nonce(16) || tag(32) || ciphertext.
def _derive(secret: str) -> tuple[bytes, bytes]:
    """Two independent keys (encrypt, mac) from one caller secret."""
    root = hashlib.sha256(_PURPOSE + (secret or "").encode()).digest()
    return (
        hashlib.sha256(b"enc" + root).digest(),
        hashlib.sha256(b"mac" + root).digest(),
    )


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(
            hmac.new(enc_key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        )
        counter += 1
    return bytes(out[:n])


def _legacy_decrypt(token: str, secret: str) -> str:
    raw = base64.urlsafe_b64decode((token or "").encode())
    if len(raw) < 48:
        raise ValueError("ciphertext too short")
    nonce, tag, ct = raw[:16], raw[16:48], raw[48:]
    enc_key, mac_key = _derive(secret)
    if not hmac.compare_digest(tag, hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("authentication failed")
    ks = _keystream(enc_key, nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks)).decode()
