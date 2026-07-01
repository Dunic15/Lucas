"""Anam client — the avatar's FACE, voiced by ElevenLabs.

(The module keeps its historical filename `tavus_client.py` so the rest of the
backend imports it unchanged; the vendor behind it is now Anam. Swapping the face
vendor only ever touches this file + frontend/avatar.html.)

We use Anam only as a mouth+face we fully control:
  - Anam's model is a *session token*: you POST a personaConfig (which face, which
    voice, the persona) and get back a short-lived `sessionToken`. The avatar page
    then streams the avatar with that token via Anam's browser SDK and calls
    `client.talk(text)` to make it speak — that is Anam's equivalent of "echo".
  - The meeting brain is still our backend: the words come from us, over the
    websocket, and the page speaks exactly those words.

To preserve the three-function contract the rest of the app expects, we map:
  create_persona(avatar)        -> the Anam avatarId (which face)   [no API call]
  create_conversation(avatar,…) -> {conversation_id, conversation_url}
                                   where conversation_url carries the sessionToken
  end_conversation(id)          -> no-op (the Anam stream ends when the page/bot
                                   leaves; Recall.leave_call already does that)

Secrets come from .env via `settings`; the per-avatar identity (face/voice) comes
from avatar.yaml. NOTE: the Anam credentials are read from the existing
TAVUS_* settings to keep this swap contained to one file — rename to ANAM_* in
config.py/.env whenever convenient.

INTEGRATION SEAM TO VERIFY on a first live Anam run:
  - the exact personaConfig field names (avatarId / voiceId / llmId / systemPrompt);
  - that frontend/avatar.html joins with Anam's SDK using conversation_url as the
    sessionToken (it currently still embeds a Daily room — update it for Anam).
"""
from __future__ import annotations

import uuid

import httpx

from .avatars import Avatar
from .config import settings

ANAM_BASE = "https://api.anam.ai/v1"


def _headers() -> dict:
    # settings.tavus_api_key holds the ANAM API key (see module note above).
    if not settings.tavus_api_key:
        raise RuntimeError("ANAM API key is not set (TAVUS_API_KEY in .env).")
    return {
        "Authorization": f"Bearer {settings.tavus_api_key}",
        "Content-Type": "application/json",
    }


def create_persona(avatar: Avatar) -> str:
    """Resolve which Anam face this avatar uses. Returns the Anam avatarId.

    Anam configures the persona inline when a session token is minted, so there
    is no separate persona-create API call — we just validate and return the face
    id here to keep the create_persona → create_conversation flow intact.
    """
    if not avatar.tavus_replica_id:
        raise RuntimeError(
            f"Avatar '{avatar.id}' has no Anam avatar id "
            "(set tavus_replica_id in avatar.yaml or TAVUS_REPLICA_ID in .env)."
        )
    if not avatar.elevenlabs_voice_id:
        raise RuntimeError(
            f"Avatar '{avatar.id}' has no voice "
            "(set elevenlabs_voice_id in avatar.yaml or ELEVENLABS_VOICE_ID in .env)."
        )
    return avatar.tavus_replica_id


def create_conversation(avatar: Avatar, persona_id: str) -> dict:
    """Mint an Anam session token for this avatar.

    Returns {conversation_id, conversation_url}, matching the previous contract:
      - conversation_id : our own routing id for the websocket (Anam doesn't
                          return one; we generate it and the page echoes it back).
      - conversation_url: the Anam sessionToken the avatar page joins with.
    """
    body = {
        "personaConfig": {
            "name": f"{avatar.name} — {avatar.role}",
            "avatarId": persona_id,                    # which face
            "voiceId": avatar.elevenlabs_voice_id,     # which voice
            # We drive speech from our backend via the page's talk()/echo, so the
            # persona's own LLM is not the source of truth. systemPrompt kept for
            # tone; omit/replace llmId per your Anam setup.
            "systemPrompt": avatar.persona_prompt or f"You are {avatar.name}.",
        }
    }
    resp = httpx.post(
        f"{ANAM_BASE}/auth/session-token",
        headers=_headers(),
        json=body,
        timeout=60.0,
    )
    resp.raise_for_status()
    session_token = resp.json()["sessionToken"]
    return {
        "conversation_id": uuid.uuid4().hex,
        "conversation_url": session_token,
    }


def end_conversation(conversation_id: str) -> None:
    """No-op for Anam.

    An Anam stream is client-side: it stops when the avatar page (the Recall bot's
    camera) leaves, which `recall_client.leave_call` already triggers on session
    end. There is no separate server-side session-token teardown to call. Kept so
    main.py's end-of-session flow stays unchanged.
    """
    return None
