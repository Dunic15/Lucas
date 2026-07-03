"""Persistent session store + transient websocket registry.

Session state survives process restarts through SQLite. Live WebSocket objects
are intentionally kept in memory only; avatar pages reconnect and re-register
against the persisted conversation routing state.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import WebSocket


def _default_store_path() -> Path:
    persistent_mount = Path("/var/data")
    if persistent_mount.exists() and os.access(persistent_mount, os.W_OK):
        return persistent_mount / "laura-store.sqlite3"
    return Path(__file__).resolve().parents[1] / "data" / "store.sqlite3"


STORE_PATH = Path(os.getenv("LAURA_STORE_PATH", str(_default_store_path())))


@dataclass
class Utterance:
    speaker: str
    text: str
    ts: float


_PERSISTED_SESSION_FIELDS = {
    "meeting_url",
    "avatar_id",
    "anam_conversation_id",
    "anam_conversation_url",
    "last_spoke_at",
    "proactive_done",
}


@dataclass
class Session:
    bot_id: str
    meeting_url: str
    avatar_id: str = "laura"
    anam_conversation_id: str = ""
    anam_conversation_url: str = ""
    transcript: list[Utterance] = field(default_factory=list)
    last_spoke_at: float = 0.0
    proactive_done: bool = False  # the one proactive flag fires at most once
    ws: WebSocket | None = None
    pending_messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _persist_enabled: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_persist_enabled", True)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        if name in _PERSISTED_SESSION_FIELDS and getattr(
            self, "_persist_enabled", False
        ):
            _persist_session(self)

    def add_utterance(self, speaker: str, text: str) -> None:
        utterance = Utterance(speaker=speaker, text=text, ts=time.time())
        self.transcript.append(utterance)
        _persist_utterance(self.bot_id, utterance)

    def transcript_text(self) -> str:
        return "\n".join(f"{u.speaker}: {u.text}" for u in self.transcript)

    def recent_transcript(self, n: int = 8) -> str:
        """The last n turns, so the avatar has the immediate meeting context."""
        return "\n".join(f"{u.speaker}: {u.text}" for u in self.transcript[-n:])

    def in_cooldown(self, cooldown_seconds: float) -> bool:
        return (time.time() - self.last_spoke_at) < cooldown_seconds

    def mark_spoke(self) -> None:
        self.last_spoke_at = time.time()


_LOCK = threading.RLock()

# bot_id -> Session
_sessions: dict[str, Session] = {}
# anam_conversation_id -> bot_id  (the avatar page only knows the conversation)
_by_conversation: dict[str, str] = {}
# bot_id -> finished post-meeting artifact (kept after the session is removed)
_artifacts: dict[str, dict] = {}
# calendar event ids we've already scheduled a bot for (avoid double-booking)
_scheduled_events: set[str] = set()


def _connect() -> sqlite3.Connection:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STORE_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                bot_id TEXT PRIMARY KEY,
                meeting_url TEXT NOT NULL,
                avatar_id TEXT NOT NULL,
                anam_conversation_id TEXT NOT NULL DEFAULT '',
                anam_conversation_url TEXT NOT NULL DEFAULT '',
                last_spoke_at REAL NOT NULL DEFAULT 0,
                proactive_done INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS utterances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                ts REAL NOT NULL,
                FOREIGN KEY(bot_id) REFERENCES sessions(bot_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_utterances_bot_id_id
                ON utterances(bot_id, id);

            CREATE TABLE IF NOT EXISTS conversation_routes (
                conversation_id TEXT PRIMARY KEY,
                bot_id TEXT NOT NULL,
                FOREIGN KEY(bot_id) REFERENCES sessions(bot_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                bot_id TEXT PRIMARY KEY,
                artifact_json TEXT NOT NULL,
                saved_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_events (
                event_id TEXT PRIMARY KEY,
                marked_at REAL NOT NULL
            );
            """
        )


def _session_from_row(row: sqlite3.Row, utterances: list[Utterance]) -> Session:
    session = Session(
        bot_id=row["bot_id"],
        meeting_url=row["meeting_url"],
        avatar_id=row["avatar_id"],
        anam_conversation_id=row["anam_conversation_id"],
        anam_conversation_url=row["anam_conversation_url"],
        last_spoke_at=float(row["last_spoke_at"]),
        proactive_done=bool(row["proactive_done"]),
    )
    object.__setattr__(session, "transcript", utterances)
    object.__setattr__(session, "ws", None)
    return session


def _load_from_db() -> None:
    with _LOCK, _connect() as conn:
        session_rows = conn.execute("SELECT * FROM sessions").fetchall()
        utterance_rows = conn.execute(
            "SELECT bot_id, speaker, text, ts FROM utterances ORDER BY id"
        ).fetchall()
        routes = conn.execute(
            "SELECT conversation_id, bot_id FROM conversation_routes"
        ).fetchall()
        artifacts = conn.execute("SELECT bot_id, artifact_json FROM artifacts").fetchall()
        scheduled = conn.execute("SELECT event_id FROM scheduled_events").fetchall()

    utterances_by_bot: dict[str, list[Utterance]] = {}
    for row in utterance_rows:
        utterances_by_bot.setdefault(row["bot_id"], []).append(
            Utterance(speaker=row["speaker"], text=row["text"], ts=float(row["ts"]))
        )

    _sessions.clear()
    _sessions.update(
        {
            row["bot_id"]: _session_from_row(
                row, utterances_by_bot.get(row["bot_id"], [])
            )
            for row in session_rows
        }
    )
    _by_conversation.clear()
    _by_conversation.update({row["conversation_id"]: row["bot_id"] for row in routes})
    _artifacts.clear()
    for row in artifacts:
        try:
            _artifacts[row["bot_id"]] = json.loads(row["artifact_json"])
        except json.JSONDecodeError:
            _artifacts[row["bot_id"]] = {}
    _scheduled_events.clear()
    _scheduled_events.update(row["event_id"] for row in scheduled)


def _persist_session(session: Session) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                bot_id, meeting_url, avatar_id, anam_conversation_id,
                anam_conversation_url, last_spoke_at, proactive_done, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET
                meeting_url=excluded.meeting_url,
                avatar_id=excluded.avatar_id,
                anam_conversation_id=excluded.anam_conversation_id,
                anam_conversation_url=excluded.anam_conversation_url,
                last_spoke_at=excluded.last_spoke_at,
                proactive_done=excluded.proactive_done,
                updated_at=excluded.updated_at
            """,
            (
                session.bot_id,
                session.meeting_url,
                session.avatar_id,
                session.anam_conversation_id,
                session.anam_conversation_url,
                float(session.last_spoke_at),
                int(session.proactive_done),
                time.time(),
            ),
        )


def _persist_utterance(bot_id: str, utterance: Utterance) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO utterances (bot_id, speaker, text, ts)
            VALUES (?, ?, ?, ?)
            """,
            (bot_id, utterance.speaker, utterance.text, float(utterance.ts)),
        )


def is_scheduled(event_id: str) -> bool:
    return event_id in _scheduled_events


def mark_scheduled(event_id: str) -> None:
    _scheduled_events.add(event_id)
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO scheduled_events (event_id, marked_at)
            VALUES (?, ?)
            """,
            (event_id, time.time()),
        )


def save_artifact(bot_id: str, artifact: dict) -> None:
    _artifacts[bot_id] = artifact
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO artifacts (bot_id, artifact_json, saved_at)
            VALUES (?, ?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET
                artifact_json=excluded.artifact_json,
                saved_at=excluded.saved_at
            """,
            (bot_id, json.dumps(artifact), time.time()),
        )


def get_artifact(bot_id: str) -> dict | None:
    return _artifacts.get(bot_id)


def create(bot_id: str, meeting_url: str, avatar_id: str = "laura") -> Session:
    s = Session(bot_id=bot_id, meeting_url=meeting_url, avatar_id=avatar_id)
    _sessions[bot_id] = s
    _persist_session(s)
    return s


def register_conversation(conversation_id: str, bot_id: str) -> None:
    _by_conversation[conversation_id] = bot_id
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO conversation_routes (conversation_id, bot_id)
            VALUES (?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET bot_id=excluded.bot_id
            """,
            (conversation_id, bot_id),
        )


def get(bot_id: str) -> Session | None:
    return _sessions.get(bot_id)


def get_by_conversation(conversation_id: str) -> Session | None:
    bot_id = _by_conversation.get(conversation_id)
    return _sessions.get(bot_id) if bot_id else None


def queue_avatar_message(session: Session, message: dict[str, Any]) -> None:
    with _LOCK:
        session.pending_messages.append(message)


def drain_avatar_messages(session: Session) -> list[dict[str, Any]]:
    with _LOCK:
        messages = list(session.pending_messages)
        session.pending_messages.clear()
        return messages


def all_sessions() -> list[Session]:
    return list(_sessions.values())


def remove(bot_id: str) -> None:
    s = _sessions.pop(bot_id, None)
    for conversation_id, routed_bot_id in list(_by_conversation.items()):
        if routed_bot_id == bot_id:
            _by_conversation.pop(conversation_id, None)

    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM conversation_routes WHERE bot_id = ?", (bot_id,))


_init_db()
_load_from_db()
