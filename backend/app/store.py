"""In-memory session store + websocket registry.

One process, one meeting at a time is fine for the MVP. For multi-tenant /
multi-meeting production, back this with Redis or a DB keyed by bot_id.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import WebSocket


@dataclass
class Utterance:
    speaker: str
    text: str
    ts: float


@dataclass
class Session:
    bot_id: str
    meeting_url: str
    avatar_id: str = "lucas"
    anam_conversation_id: str = ""
    anam_conversation_url: str = ""
    transcript: list[Utterance] = field(default_factory=list)
    last_spoke_at: float = 0.0
    ws: WebSocket | None = None

    def add_utterance(self, speaker: str, text: str) -> None:
        self.transcript.append(Utterance(speaker=speaker, text=text, ts=time.time()))

    def transcript_text(self) -> str:
        return "\n".join(f"{u.speaker}: {u.text}" for u in self.transcript)

    def in_cooldown(self, cooldown_seconds: float) -> bool:
        return (time.time() - self.last_spoke_at) < cooldown_seconds

    def mark_spoke(self) -> None:
        self.last_spoke_at = time.time()


# bot_id -> Session
_sessions: dict[str, Session] = {}
# anam_conversation_id -> bot_id  (the avatar page only knows the conversation)
_by_conversation: dict[str, str] = {}
# bot_id -> finished post-meeting artifact (kept after the session is removed)
_artifacts: dict[str, dict] = {}


def save_artifact(bot_id: str, artifact: dict) -> None:
    _artifacts[bot_id] = artifact


def get_artifact(bot_id: str) -> dict | None:
    return _artifacts.get(bot_id)


def create(bot_id: str, meeting_url: str, avatar_id: str = "lucas") -> Session:
    s = Session(bot_id=bot_id, meeting_url=meeting_url, avatar_id=avatar_id)
    _sessions[bot_id] = s
    return s


def register_conversation(conversation_id: str, bot_id: str) -> None:
    _by_conversation[conversation_id] = bot_id


def get(bot_id: str) -> Session | None:
    return _sessions.get(bot_id)


def get_by_conversation(conversation_id: str) -> Session | None:
    bot_id = _by_conversation.get(conversation_id)
    return _sessions.get(bot_id) if bot_id else None


def all_sessions() -> list[Session]:
    return list(_sessions.values())


def remove(bot_id: str) -> None:
    s = _sessions.pop(bot_id, None)
    if s and s.anam_conversation_id:
        _by_conversation.pop(s.anam_conversation_id, None)
