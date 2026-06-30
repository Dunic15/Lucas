"""Tavus client — the avatar's FACE, voiced by ElevenLabs.

We use Tavus only as a mouth+face we fully control:
  - A persona pins the replica (face) and the TTS engine to ElevenLabs with
    the avatar's chosen voice id. (This is how ElevenLabs becomes the voice.)
  - A conversation gives us a `conversation_url` (a Daily room) that the avatar
    page embeds and that Recall renders as the bot's camera.
  - We make the avatar speak via an "echo" interaction sent into the Daily room
    from the avatar page (see frontend/avatar.html). The brain decides the
    words; Tavus just renders + voices them.

Secrets (API keys) come from .env via `settings`. The per-avatar *identity* —
which face and which voice — comes from avatar.yaml. So each avatar can look and
sound different, while keys stay in one place.
"""
from __future__ import annotations

import httpx

from .avatars import Avatar
from .config import settings

TAVUS_BASE = "https://tavusapi.com/v2"


def _headers() -> dict:
    if not settings.tavus_api_key:
        raise RuntimeError("TAVUS_API_KEY is not set.")
    return {"x-api-key": settings.tavus_api_key, "Content-Type": "application/json"}


def create_persona(avatar: Avatar) -> str:
    """Create a persona whose voice is ElevenLabs and whose brain is external.

    `pipeline_mode: echo` means Tavus renders/voices exactly the text we send;
    it does not run STT/LLM itself. The meeting brain is our backend.
    """
    if not avatar.elevenlabs_voice_id:
        raise RuntimeError(
            f"Avatar '{avatar.id}' has no ElevenLabs voice "
            "(set elevenlabs_voice_id in avatar.yaml or ELEVENLABS_VOICE_ID in .env)."
        )

    body = {
        "persona_name": f"{avatar.name} — {avatar.role}",
        "pipeline_mode": "echo",
        "default_replica_id": avatar.tavus_replica_id,
        "layers": {
            # ElevenLabs is the voice.
            "tts": {
                "tts_engine": "elevenlabs",
                "api_key": settings.elevenlabs_api_key,
                "external_voice_id": avatar.elevenlabs_voice_id,
            },
        },
    }
    resp = httpx.post(
        f"{TAVUS_BASE}/personas", headers=_headers(), json=body, timeout=60.0
    )
    resp.raise_for_status()
    return resp.json()["persona_id"]


def create_conversation(avatar: Avatar, persona_id: str) -> dict:
    """Start a live avatar session. Returns {conversation_id, conversation_url}."""
    body = {
        "persona_id": persona_id,
        "replica_id": avatar.tavus_replica_id,
        "conversation_name": f"{avatar.name} session",
    }
    resp = httpx.post(
        f"{TAVUS_BASE}/conversations", headers=_headers(), json=body, timeout=60.0
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "conversation_id": data["conversation_id"],
        "conversation_url": data["conversation_url"],
    }


def end_conversation(conversation_id: str) -> None:
    """End the avatar session (stops Tavus per-minute billing)."""
    httpx.post(
        f"{TAVUS_BASE}/conversations/{conversation_id}/end",
        headers=_headers(),
        timeout=30.0,
    )
