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

import httpx

from .config import settings


def _headers() -> dict:
    if not settings.recall_api_key:
        raise RuntimeError("RECALL_API_KEY is not set.")
    return {
        "Authorization": f"Token {settings.recall_api_key}",
        "Content-Type": "application/json",
    }


def create_bot(meeting_url: str, avatar_page_url: str) -> dict:
    """Send a bot into `meeting_url` showing `avatar_page_url` on its camera.

    Returns the created bot object (includes its `id`).
    """
    webhook_url = f"{settings.public_base_url.rstrip('/')}/webhooks/recall"

    body = {
        "meeting_url": meeting_url,
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
            # The bot's camera renders our avatar page (Anam face lives inside).
            "output_media": {
                "kind": "webpage",
                "url": avatar_page_url,
            },
        },
    }

    resp = httpx.post(
        f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/",
        headers=_headers(),
        json=body,
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


def leave_call(bot_id: str) -> None:
    """Remove the bot from the meeting (stops avatar streaming → stops billing)."""
    httpx.post(
        f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/{bot_id}/leave_call/",
        headers=_headers(),
        timeout=30.0,
    )
