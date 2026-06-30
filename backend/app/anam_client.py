"""Anam client -- the avatar's face and voice.

The meeting brain stays in our backend. Anam is the realtime video/voice
renderer: the browser opens an Anam WebRTC stream, then the backend sends
approved lines over our websocket and the page calls `anamClient.talk(text)`.

Secrets stay server-side. The browser only receives a short-lived Anam session
token for the current local avatar session.
"""
from __future__ import annotations

import httpx

from .avatars import Avatar
from .config import settings

ANAM_BASE = "https://api.anam.ai/v1"
CLIENT_CONTROLLED_LLM = "CUSTOMER_CLIENT_V1"


def _headers() -> dict:
    if not settings.anam_api_key:
        raise RuntimeError("ANAM_API_KEY is not set.")
    return {
        "Authorization": f"Bearer {settings.anam_api_key}",
        "Content-Type": "application/json",
    }


def _voice_generation_options() -> dict:
    """Optional ElevenLabs voice tuning passed through Anam."""
    options = {}
    if settings.anam_voice_stability is not None:
        options["stability"] = settings.anam_voice_stability
    if settings.anam_voice_similarity_boost is not None:
        options["similarityBoost"] = settings.anam_voice_similarity_boost
    if settings.anam_voice_speed is not None:
        options["speed"] = settings.anam_voice_speed
    if settings.anam_voice_use_speaker_boost is not None:
        options["useSpeakerBoost"] = settings.anam_voice_use_speaker_boost
    if settings.anam_voice_style is not None:
        options["style"] = settings.anam_voice_style
    return options


def create_session_token(avatar: Avatar) -> str:
    """Create a short-lived browser token for one Anam persona session."""
    if not avatar.anam_avatar_id:
        raise RuntimeError(
            f"Avatar '{avatar.id}' has no Anam avatar id "
            "(set anam_avatar_id in avatar.yaml or ANAM_AVATAR_ID in .env)."
        )
    if not avatar.anam_voice_id:
        raise RuntimeError(
            f"Avatar '{avatar.id}' has no Anam voice id "
            "(set anam_voice_id in avatar.yaml or ANAM_VOICE_ID in .env)."
        )

    persona_config = {
        "name": avatar.name,
        "avatarId": avatar.anam_avatar_id,
        "avatarModel": avatar.anam_avatar_model,
        # Use an Anam voice id. For ElevenLabs, import/select the voice in
        # Anam Lab and put the resulting Anam voice id in ANAM_VOICE_ID.
        "voiceId": avatar.anam_voice_id,
        # Our backend decides what to say; the browser sends it via talk().
        "llmId": settings.anam_llm_id or CLIENT_CONTROLLED_LLM,
        "systemPrompt": avatar.persona_prompt,
    }
    voice_options = _voice_generation_options()
    if voice_options:
        persona_config["voiceGenerationOptions"] = voice_options

    body = {"personaConfig": persona_config}
    resp = httpx.post(
        f"{ANAM_BASE}/auth/session-token", headers=_headers(), json=body, timeout=60.0
    )
    resp.raise_for_status()
    return resp.json()["sessionToken"]
