"""Tavus client — the avatar's FACE, voiced by ElevenLabs.

We use Tavus only as a mouth+face we fully control:
  - A persona pins the replica (face) and the TTS engine to ElevenLabs with
    our chosen voice id. (This is how ElevenLabs becomes the voice.)
  - A conversation gives us a `conversation_url` (a Daily room) that the avatar
    page embeds and that Recall renders as the bot's camera.
  - We make the avatar speak via an "echo" interaction sent into the Daily room
    from the avatar page (see frontend/avatar.html). The brain decides the
    words; Tavus just renders + voices them.

Because the brain lives in our backend, the persona's own LLM layer is left in
"echo" pipeline mode — Tavus does not run its own conversation loop.
"""
from __future__ import annotations

import httpx

from .config import settings

TAVUS_BASE = "https://tavusapi.com/v2"


def _headers() -> dict:
    if not settings.tavus_api_key:
        raise RuntimeError("TAVUS_API_KEY is not set.")
    return {"x-api-key": settings.tavus_api_key, "Content-Type": "application/json"}


def ensure_persona() -> str:
    """Return a persona id, creating one (ElevenLabs voice) if not preset."""
    if settings.tavus_persona_id:
        return settings.tavus_persona_id
    return create_persona()


def create_persona() -> str:
    """Create a persona whose voice is ElevenLabs and whose brain is external.

    `pipeline_mode: echo` means Tavus renders/voices exactly the text we send;
    it does not run STT/LLM itself. The meeting brain is our backend.
    """
    if not settings.elevenlabs_api_key or not settings.elevenlabs_voice_id:
        raise RuntimeError("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are required.")

    body = {
        "persona_name": "Sofia — AI Process Expert",
        "pipeline_mode": "echo",
        "default_replica_id": settings.tavus_replica_id,
        "layers": {
            # ElevenLabs is the voice.
            "tts": {
                "tts_engine": "elevenlabs",
                "api_key": settings.elevenlabs_api_key,
                "external_voice_id": settings.elevenlabs_voice_id,
            },
        },
    }
    resp = httpx.post(
        f"{TAVUS_BASE}/personas", headers=_headers(), json=body, timeout=60.0
    )
    resp.raise_for_status()
    return resp.json()["persona_id"]


def create_conversation(persona_id: str, name: str = "Process Avatar Session") -> dict:
    """Start a live avatar session. Returns {conversation_id, conversation_url}."""
    body = {
        "persona_id": persona_id,
        "replica_id": settings.tavus_replica_id,
        "conversation_name": name,
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
