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

    body = {
        "personaConfig": {
            "name": avatar.name,
            "avatarId": avatar.anam_avatar_id,
            "avatarModel": avatar.anam_avatar_model,
            "voiceId": avatar.anam_voice_id,
            # Our backend decides what to say; the browser sends it via talk().
            "llmId": settings.anam_llm_id or CLIENT_CONTROLLED_LLM,
            "systemPrompt": avatar.persona_prompt,
        }
    }
    resp = httpx.post(
        f"{ANAM_BASE}/auth/session-token", headers=_headers(), json=body, timeout=60.0
    )
    resp.raise_for_status()
    return resp.json()["sessionToken"]
