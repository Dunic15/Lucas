"""Anam client — the avatar's FACE, voiced by ElevenLabs.

Swapping the face vendor only ever touches this file + frontend/avatar.html.

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

Secrets come from .env via `settings` (ANAM_API_KEY); the per-avatar identity
(face/voice) comes from avatar.yaml (anam_avatar_id / elevenlabs_voice_id).

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

# Reuse one connection pool across calls so we don't pay a fresh TLS handshake
# on every persona fetch / session-token mint (shaves latency off session start).
_client = httpx.Client(
    timeout=60.0,
    limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=60.0),
)


def _headers() -> dict:
    if not settings.anam_api_key:
        raise RuntimeError("ANAM_API_KEY is not set.")
    return {
        "Authorization": f"Bearer {settings.anam_api_key}",
        "Content-Type": "application/json",
    }


def create_persona(avatar: Avatar) -> str:
    """Resolve which Anam face this avatar uses. Returns the Anam avatarId.

    Anam configures the persona inline when a session token is minted, so there
    is no separate persona-create API call — we just validate and return the face
    id here to keep the create_persona → create_conversation flow intact.
    """
    if not avatar.anam_avatar_id:
        raise RuntimeError(
            f"Avatar '{avatar.id}' has no Anam avatar id "
            "(set anam_avatar_id in avatar.yaml or ANAM_AVATAR_ID in .env)."
        )
    # Voice is optional: if no voice id is set, Anam uses the persona's default
    # voice ("use Anam voice for now"). Set ELEVENLABS_VOICE_ID to override.
    return avatar.anam_avatar_id


# Keep the Anam avatar passive: our backend (RAG + Claude) is the single brain and
# drives every spoken line via talk(). Anam still REQUIRES an llmId + systemPrompt to
# mint a modern (non-legacy) session token, so we can't drop the brain — instead we
# neutralize it with a system prompt that tells it to never speak on its own. It only
# says what we send it.
_MOUTH_SYSTEM_PROMPT = (
    "You are a video avatar whose speech is controlled entirely by an external "
    "system. Never start talking, never greet, and never answer questions on your "
    "own initiative. Do not respond to anything you see or hear. Remain silent at "
    "all times unless the controlling system explicitly sends you exact words to "
    "speak, in which case say only those words verbatim and nothing else."
)


def _expand_persona(persona_id: str) -> dict:
    """Turn a SAVED Anam persona into an inline personaConfig.

    Anam requires `avatarId`, `voiceId`, `llmId`, and `systemPrompt` for a modern
    (non-legacy) session token — omitting the llmId/systemPrompt makes Anam fall
    back to a legacy token, which the SDK now rejects. So we re-emit the persona's
    face + voice + its llmId, but OVERRIDE the system prompt with a keep-silent
    instruction and drop its knowledge tools: the avatar won't autonomously answer
    (that fought our backend), it just speaks the exact lines we drive via talk().
    """
    p = _client.get(f"{ANAM_BASE}/personas/{persona_id}", headers=_headers(), timeout=30.0)
    p.raise_for_status()
    d = p.json()
    cfg = {
        "name": d.get("name") or "Assistant",
        "avatarId": (d.get("avatar") or {}).get("id"),
        "voiceId": (d.get("voice") or {}).get("id"),
        "llmId": d.get("llmId"),  # required by Anam for a non-legacy token
        "systemPrompt": _MOUTH_SYSTEM_PROMPT,  # neutralized: never speaks on its own
        "skipGreeting": True,
        # Fully passive: 0 disables the idle "please say something" nudge AND the
        # auto-disconnect on silence, so she only ever speaks lines we drive.
        "voiceDetectionOptions": {
            "silenceBeforeSkipTurnSeconds": 0,
            "silenceBeforeSessionEndSeconds": 0,
        },
    }
    # Drop empty/None values so we don't send nulls Anam may reject (but keep the
    # voiceDetectionOptions dict, whose 0 values are meaningful).
    return {k: v for k, v in cfg.items() if v or k == "voiceDetectionOptions"}


def create_conversation(avatar: Avatar, persona_id: str) -> dict:
    """Mint an Anam session token for this avatar.

    Returns {conversation_id, conversation_url}, matching the previous contract:
      - conversation_id : our own routing id for the websocket (Anam doesn't
                          return one; we generate it and the page echoes it back).
      - conversation_url: the Anam sessionToken the avatar page joins with.
    """
    body = {"personaConfig": _expand_persona(persona_id)}
    resp = _client.post(
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
