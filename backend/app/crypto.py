"""Tiny authenticated symmetric encryption for secrets at rest — stdlib only.

Used to keep the per-org Google refresh token out of plaintext in the store
(docs/product/NATIVE-INTEGRATIONS-PLAN.md: "persist encrypted, never in git").
The runtime image ships neither ``cryptography`` nor ``pynacl``, so this is a
dependency-free encrypt-then-MAC construction: an HMAC-SHA256 keystream in
counter mode XOR'd with the plaintext, plus an HMAC-SHA256 tag over
(nonce || ciphertext). It is deliberately small and self-contained; it protects
a token if the SQLite file leaks, and it is NOT a replacement for a real KMS —
a production hardening (AWS KMS / SSM SecureString, as the Cedric bearer uses)
is the documented next step, and callers can swap the key source without
touching call sites.

Format (all base64url, one string): nonce(16) || tag(32) || ciphertext.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct

_PURPOSE = b"laura-secret-v1:"


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


def encrypt(plaintext: str, secret: str) -> str:
    """Encrypt ``plaintext`` under ``secret``; returns an opaque base64url token."""
    enc_key, mac_key = _derive(secret)
    nonce = os.urandom(16)
    pt = (plaintext or "").encode()
    ks = _keystream(enc_key, nonce, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, ks))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + ct).decode()


def decrypt(token: str, secret: str) -> str:
    """Inverse of :func:`encrypt`. Raises ValueError on a bad/forged token
    (wrong key, truncation, tampering) — callers treat that as "no token"."""
    raw = base64.urlsafe_b64decode((token or "").encode())
    if len(raw) < 48:
        raise ValueError("ciphertext too short")
    nonce, tag, ct = raw[:16], raw[16:48], raw[48:]
    enc_key, mac_key = _derive(secret)
    if not hmac.compare_digest(tag, hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("authentication failed")
    ks = _keystream(enc_key, nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks)).decode()
