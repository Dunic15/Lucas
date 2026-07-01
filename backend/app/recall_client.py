"""Recall.ai client — gets the bot into the meeting and streams transcript back.

The bot does two things for us:
  1. Renders our avatar page as its camera (output_media kind=webpage).
  2. Streams finalized transcript utterances to our webhook (transcript.data),
     each tagged with the speaking participant's name (Recall's built-in
     diarization — no separate speaker-ID service needed for the MVP).

Field shapes follow Recall's current Create Bot schema. If your Recall API
version differs, the request body is isolated here — adjust in one place.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from urllib.parse import urlparse

import httpx

from .config import settings


def readiness() -> dict:
    """Return non-secret Recall setup status for diagnostics."""
    issues: list[str] = []
    key = settings.recall_api_key.strip()
    public_base_url = settings.public_base_url.strip()

    if not key:
        issues.append("RECALL_API_KEY is not set.")
        key_status = "missing"
    elif key.startswith("whsec_"):
        issues.append(
            "RECALL_API_KEY starts with whsec_, which is a webhook verification "
            "secret, not a Recall API key."
        )
        key_status = "webhook_secret"
    else:
        key_status = "set"

    parsed = urlparse(public_base_url)
    if not public_base_url:
        issues.append("PUBLIC_BASE_URL is not set.")
        public_url_status = "missing"
    elif "your-" in public_base_url or "placeholder" in public_base_url.lower():
        issues.append("PUBLIC_BASE_URL still looks like a placeholder.")
        public_url_status = "placeholder"
    elif parsed.scheme != "https":
        issues.append("PUBLIC_BASE_URL must be an HTTPS URL reachable by Recall.")
        public_url_status = "not_https"
    elif parsed.hostname in {"127.0.0.1", "localhost"}:
        issues.append("PUBLIC_BASE_URL cannot be localhost; use an ngrok/deploy URL.")
        public_url_status = "local"
    else:
        public_url_status = "set"

    return {
        "ready": not issues,
        "issues": issues,
        "api_base": settings.recall_api_base.rstrip("/"),
        "api_key": key_status,
        "public_base_url": public_url_status,
    }


def assert_ready() -> None:
    """Raise a user-actionable error before creating paid/live resources."""
    status = readiness()
    if status["ready"]:
        return
    raise RuntimeError("Recall is not ready: " + " ".join(status["issues"]))


def _api_key() -> str:
    key = settings.recall_api_key.strip()
    if not key:
        raise RuntimeError("RECALL_API_KEY is not set.")
    if key.startswith("whsec_"):
        raise RuntimeError(
            "RECALL_API_KEY starts with whsec_, which is a webhook verification "
            "secret, not a Recall API key."
        )
    return key


def _headers() -> dict:
    assert_ready()
    return {
        # Recall accepts the raw API key; using no prefix makes it harder to
        # confuse API keys with webhook secrets in debugging.
        "Authorization": _api_key(),
        "Content-Type": "application/json",
    }


def auth_check() -> dict:
    """Make a safe read-only API call to confirm Recall authentication."""
    status = readiness()
    if status["api_key"] != "set":
        return {**status, "auth": "skipped"}

    try:
        resp = httpx.get(
            f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/",
            headers={
                "Authorization": _api_key(),
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )
    except Exception as e:
        return {**status, "ready": False, "auth": "error", "error": str(e)}

    if 200 <= resp.status_code < 300:
        return {**status, "auth": "valid"}
    if resp.status_code == 401:
        return {
            **status,
            "ready": False,
            "auth": "invalid",
            "issues": status["issues"] + [
                "Recall rejected RECALL_API_KEY with 401. Check the key and region."
            ],
        }
    return {
        **status,
        "ready": False,
        "auth": "unexpected_status",
        "status_code": resp.status_code,
    }


def verify_webhook(raw_body: bytes, headers: Mapping[str, str]) -> None:
    """Verify a Recall webhook if RECALL_WEBHOOK_SECRET is configured.

    Recall's whsec_ value is for HMAC verification of incoming webhook requests.
    It is intentionally separate from RECALL_API_KEY, which authenticates API
    calls that create or control bots.
    """
    secret = settings.recall_webhook_secret.strip()
    if not secret:
        return
    if not secret.startswith("whsec_"):
        raise RuntimeError("RECALL_WEBHOOK_SECRET must start with whsec_.")

    msg_id = headers.get("webhook-id") or headers.get("svix-id")
    timestamp = headers.get("webhook-timestamp") or headers.get("svix-timestamp")
    signature_header = (
        headers.get("webhook-signature") or headers.get("svix-signature")
    )
    if not msg_id or not timestamp or not signature_header:
        raise RuntimeError("Recall webhook signature headers are missing.")

    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed_payload = b".".join([msg_id.encode(), timestamp.encode(), raw_body])
    expected = hmac.new(key, signed_payload, hashlib.sha256).digest()

    for versioned_sig in signature_header.split():
        try:
            version, encoded_sig = versioned_sig.split(",", 1)
        except ValueError:
            continue
        if version != "v1":
            continue
        try:
            actual = base64.b64decode(encoded_sig)
        except Exception:
            continue
        if len(actual) == len(expected) and hmac.compare_digest(actual, expected):
            return

    raise RuntimeError("Recall webhook signature verification failed.")


def create_bot(
    meeting_url: str, avatar_page_url: str, join_at: str | None = None
) -> dict:
    """Send a bot into `meeting_url` showing `avatar_page_url` on its camera.

    If `join_at` (ISO 8601, >=10 min in the future) is given, Recall SCHEDULES the
    bot to join then — this is how calendar auto-join dispatches bots ahead of time.
    Returns the created bot object (includes its `id`).
    """
    webhook_url = f"{settings.public_base_url.rstrip('/')}/webhooks/recall"

    body = {
        "meeting_url": meeting_url,
        "bot_name": "Lucas",
        "recording_config": {
            "transcript": {
                "provider": {
                    "recallai_streaming": {
                        "mode": "prioritize_low_latency",
                        "language_code": "en",
                    }
                },
            },
            # Real-time transcript utterances delivered here.
            "realtime_endpoints": [
                {
                    "type": "webhook",
                    "url": webhook_url,
                    "events": ["transcript.data"],
                }
            ],
        },
        # Top-level: the bot's camera renders our avatar page (Anam face inside).
        # Shape per Recall's Output Media API: camera → kind=webpage → config.url.
        "output_media": {
            "camera": {
                "kind": "webpage",
                "config": {"url": avatar_page_url},
            }
        },
    }
    if join_at:  # schedule the bot to join at this time instead of now
        body["join_at"] = join_at

    resp = httpx.post(
        f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/",
        headers=_headers(),
        json=body,
        timeout=60.0,
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise RuntimeError(
                "Recall rejected RECALL_API_KEY with 401. Make sure it is the "
                "API Key, not a whsec_ workspace/webhook secret, and that "
                "RECALL_API_BASE matches the key's region."
            ) from e
        raise
    return resp.json()


def leave_call(bot_id: str) -> None:
    """Remove the bot from the meeting (stops avatar streaming → stops billing)."""
    httpx.post(
        f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/{bot_id}/leave_call/",
        headers=_headers(),
        timeout=30.0,
    )
