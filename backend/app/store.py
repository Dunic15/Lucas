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
    "org_id",
    "anam_conversation_id",
    "anam_conversation_url",
    "last_spoke_at",
    "proactive_done",
    "integration",
}


@dataclass
class Session:
    bot_id: str
    meeting_url: str
    avatar_id: str = "laura"
    # Owning tenant (auth.py user's org). "" = shared/service session — the
    # pre-auth world, calendar auto-join, and orchestrator (Cedric) starts.
    # org_id == user_id today; the seam is what matters (MULTI-TENANCY.md).
    org_id: str = ""
    anam_conversation_id: str = ""
    anam_conversation_url: str = ""
    transcript: list[Utterance] = field(default_factory=list)
    last_spoke_at: float = 0.0
    proactive_done: bool = False  # the one proactive flag fires at most once
    # Orchestrator (Cedric) integration state for this session:
    # {callback_url, context_url, external_ref, brief, meeting, context_refreshed}.
    # None = plain session with no orchestrator attached. Persisted as JSON so a
    # mid-meeting restart still knows where to deliver the artifact.
    integration: dict | None = None
    ws: WebSocket | None = None
    pending_messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # Live MeetingState (see meeting_state.py). In-memory only — it is derived
    # entirely from the persisted transcript, so after a restart it is rebuilt
    # by replaying the utterances rather than persisted (transcript is PII;
    # one copy in the DB is enough).
    meeting_state: Any = field(default=None, repr=False, compare=False)
    # Cross-meeting carryover brief (ledger.carryover_brief). In-memory only:
    # None = not loaded yet (load lazily), "" = loaded, no history.
    memory_brief: Any = field(default=None, repr=False, compare=False)
    # Until when (epoch seconds) the avatar is estimated to still be speaking —
    # drives barge-in (a human talking inside this window interrupts her).
    speaking_until: float = field(default=0.0, repr=False, compare=False)
    # Monotonic speech-turn counter. Every stop (barge-in) and every new answer
    # turn bumps it; speak messages are stamped with the generation they belong
    # to, so a cancelled turn's late sentences can be dropped on BOTH sides
    # (the streaming loop breaks, the page discards stale generations).
    # In-memory only: a restart naturally cancels any in-flight turn.
    speech_generation: int = field(default=0, repr=False, compare=False)
    # Running notes of the meeting OLDER than the recent-history window, kept
    # fresh in the background (see main._refresh_rolling_summary). Gives the
    # live brain the whole meeting's arc without widening the hot-path prompt.
    # In-memory only — derived from the persisted transcript (PII stays put).
    rolling_summary: str = field(default="", repr=False, compare=False)
    summary_upto: int = field(default=0, repr=False, compare=False)
    summarizing: bool = field(default=False, repr=False, compare=False)
    # When she last spoke an acknowledgment ("Mm-hm.") — set by the partial-
    # transcript path so the final-utterance path doesn't ack the same turn
    # twice. In-memory only: an ack is worthless across a restart.
    last_ack_at: float = field(default=0.0, repr=False, compare=False)
    # When she last backchanneled ("Mm-hm." while a human talks) — keeps the
    # listening cue rare. In-memory only, like the ack timestamp.
    last_backchannel_at: float = field(default=0.0, repr=False, compare=False)
    # Recently spoken lines (normalized text -> epoch seconds) for the
    # repetition guard: never say the same line twice within the window.
    _recent_lines: dict = field(default_factory=dict, repr=False, compare=False)
    # In-meeting map: anonymous participant id -> stable "Guest N" label. In-memory
    # only (like ws/pending_messages); on a mid-meeting restart numbering may
    # restart, which is harmless — distinct callers still stay distinct.
    _anon_labels: dict[str, str] = field(
        default_factory=dict, repr=False, compare=False
    )
    # Live roster from Recall participant_events: participant id -> {"name",
    # "here"}. Covers people who never speak (the transcript alone can't).
    # In-memory only; after a restart roster() falls back to transcript
    # speakers until the next join/leave event re-seeds it.
    participants: dict = field(default_factory=dict, repr=False, compare=False)
    # When a HUMAN partial transcript last arrived — the deference window
    # checks it to see whether someone started answering a room-open question
    # while she politely waited. In-memory only.
    last_human_partial_at: float = field(default=0.0, repr=False, compare=False)
    # One-shot flag: the wrap-up nudge to a silent participant fires at most
    # once per meeting. In-memory only (a restart forgiving a second nudge is
    # harmless).
    quiet_nudge_done: bool = field(default=False, repr=False, compare=False)
    # Opening settle-in ("wait to be called"): she stays silent unless directly
    # addressed for the first settings.opening_grace_seconds after joining, so
    # she never talks over the room while it settles (hellos, "can you hear me?",
    # late joiners). `addressed_once` ends the grace early the instant she's
    # first named. Both in-memory only: a mid-meeting restart harmlessly
    # re-applies the short grace (not interrupting right after reconnecting is,
    # if anything, desirable).
    created_at: float = field(default_factory=time.time, repr=False, compare=False)
    addressed_once: bool = field(default=False, repr=False, compare=False)
    # Hand-raise etiquette: epoch seconds since her hand went up (0 = down) and
    # the grounded contribution she queued instead of speaking over the room.
    # Delivered when someone invites her ("dimmi, Laura"), dropped on timeout.
    # In-memory only: after a restart the hand is simply down again.
    hand_raised_at: float = field(default=0.0, repr=False, compare=False)
    pending_contribution: str = field(default="", repr=False, compare=False)
    _persist_enabled: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_persist_enabled", True)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        if name in _PERSISTED_SESSION_FIELDS and getattr(
            self, "_persist_enabled", False
        ):
            _persist_session(self)

    def resolve_speaker(self, name: str | None, participant_id: Any = None) -> str:
        """Human-readable speaker label for a transcript utterance.

        Named participants keep their real name (Recall gets it from the meeting
        platform login — stable across meetings, no voice ID needed). Anonymous
        participants (phone dial-ins, unnamed guests) have no name but DO carry a
        stable per-meeting participant id, so map each distinct id to its own
        "Guest N" — otherwise two silent callers both collapse into one label and
        the avatar can't tell them apart.
        """
        name = (name or "").strip()
        if name:
            return name
        key = "" if participant_id is None else str(participant_id)
        if not key:
            return "Guest"
        label = self._anon_labels.get(key)
        if label is None:
            label = f"Guest {len(self._anon_labels) + 1}"
            self._anon_labels[key] = label
        return label

    def participant_event(
        self, name: str | None, participant_id: Any, *, here: bool
    ) -> str:
        """Fold a Recall participant_events.join/leave into the live roster."""
        label = self.resolve_speaker(name, participant_id)
        key = str(participant_id) if participant_id is not None else label
        self.participants[key] = {"name": label, "here": here}
        return label

    def present_names(self) -> list[str]:
        """Names currently in the room per the EVENT roster only — cheap (small
        dict, no transcript scan), safe to call on the partial hot path. Used
        to keep the fuzzy wake from swallowing a real participant's name."""
        return [p["name"] for p in self.participants.values() if p.get("here")]

    def roster(self, avatar_name: str = "") -> list[str]:
        """Who is in the meeting right now, besides the avatar itself.

        Prefers the event-driven roster (it sees silent participants); merges in
        transcript speakers as a net for missed events / process restarts.
        """
        skip = {avatar_name.strip().lower(), "laura", ""}
        names: list[str] = []
        for p in self.participants.values():
            if p.get("here") and p["name"].strip().lower() not in skip:
                names.append(p["name"])
        gone = {
            p["name"].strip().lower()
            for p in self.participants.values()
            if not p.get("here")
        }
        seen = {n.strip().lower() for n in names}
        for u in self.transcript:
            low = u.speaker.strip().lower()
            if low not in skip and low not in seen and low not in gone:
                seen.add(low)
                names.append(u.speaker)
        return names

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
                integration_json TEXT NOT NULL DEFAULT '',
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

            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '',
                picture TEXT NOT NULL DEFAULT '',
                org_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_login_at REAL NOT NULL
            );

            -- Per-org avatar connections (the Configure tab). One row per
            -- (org, avatar, provider); config_json holds NON-SECRET wiring
            -- only (e.g. the brain's Slack team_id + default channel) —
            -- minted credentials live in the env/SSM registry, never here.
            CREATE TABLE IF NOT EXISTS org_connections (
                org_id TEXT NOT NULL,
                avatar_id TEXT NOT NULL,
                provider TEXT NOT NULL,      -- cedric-brain | gmail | calendar | slack | drive
                status TEXT NOT NULL,        -- connected | pending | disconnected
                config_json TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY (org_id, avatar_id, provider)
            );
            """
        )
        # Migration for stores created before the Cedric integration column.
        try:
            conn.execute(
                "ALTER TABLE sessions "
                "ADD COLUMN integration_json TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migration for stores created before per-user session ownership.
        try:
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN org_id TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # column already exists


def _session_from_row(row: sqlite3.Row, utterances: list[Utterance]) -> Session:
    integration = None
    if "integration_json" in row.keys() and row["integration_json"]:
        try:
            integration = json.loads(row["integration_json"])
        except json.JSONDecodeError:
            integration = None
    session = Session(
        bot_id=row["bot_id"],
        meeting_url=row["meeting_url"],
        avatar_id=row["avatar_id"],
        org_id=row["org_id"] if "org_id" in row.keys() else "",
        anam_conversation_id=row["anam_conversation_id"],
        anam_conversation_url=row["anam_conversation_url"],
        last_spoke_at=float(row["last_spoke_at"]),
        proactive_done=bool(row["proactive_done"]),
        integration=integration,
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
                bot_id, meeting_url, avatar_id, org_id, anam_conversation_id,
                anam_conversation_url, last_spoke_at, proactive_done,
                integration_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET
                meeting_url=excluded.meeting_url,
                avatar_id=excluded.avatar_id,
                org_id=excluded.org_id,
                anam_conversation_id=excluded.anam_conversation_id,
                anam_conversation_url=excluded.anam_conversation_url,
                last_spoke_at=excluded.last_spoke_at,
                proactive_done=excluded.proactive_done,
                integration_json=excluded.integration_json,
                updated_at=excluded.updated_at
            """,
            (
                session.bot_id,
                session.meeting_url,
                session.avatar_id,
                session.org_id,
                session.anam_conversation_id,
                session.anam_conversation_url,
                float(session.last_spoke_at),
                int(session.proactive_done),
                json.dumps(session.integration) if session.integration else "",
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


def list_artifacts() -> list[dict]:
    """Every saved artifact with its metadata, newest first (meetings page).
    Reads the DB (not the in-memory cache) so it sees rows written by other
    processes — e.g. tests or scripts seeding the store."""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT bot_id, artifact_json, saved_at FROM artifacts ORDER BY saved_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        try:
            artifact = json.loads(r["artifact_json"])
        except (TypeError, ValueError):
            artifact = {}
        out.append({"bot_id": r["bot_id"], "saved_at": r["saved_at"], "artifact": artifact})
    return out


def create(
    bot_id: str, meeting_url: str, avatar_id: str = "laura", org_id: str = ""
) -> Session:
    s = Session(
        bot_id=bot_id, meeting_url=meeting_url, avatar_id=avatar_id, org_id=org_id
    )
    _sessions[bot_id] = s
    _persist_session(s)
    return s


# ── users (auth.py) ────────────────────────────────────────────────────
# user_id is DERIVED from the email (sha256 prefix), so the same person gets
# the same user_id — and therefore the same org_id and the same artifacts —
# even if the ephemeral SQLite store is wiped by a redeploy. org_id == user_id
# today (personal orgs); the column is the multi-tenancy seam.

def user_id_for_email(email: str) -> str:
    import hashlib

    normalized = (email or "").strip().lower()
    return "u_" + hashlib.sha256(normalized.encode()).hexdigest()[:16]


def upsert_user(email: str, name: str = "", picture: str = "") -> dict:
    """Create-or-refresh a user row at login. Returns the user dict + created."""
    email = (email or "").strip().lower()
    uid = user_id_for_email(email)
    now = time.time()
    with _LOCK, _connect() as conn:
        created = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (uid,)
        ).fetchone() is None
        conn.execute(
            """
            INSERT INTO users (user_id, email, name, picture, org_id,
                               created_at, last_login_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                picture=excluded.picture,
                last_login_at=excluded.last_login_at
            """,
            (uid, email, name, picture, uid, now, now),
        )
    return {"user_id": uid, "email": email, "name": name, "picture": picture,
            "org_id": uid, "created": created}


def get_user(user_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT user_id, email, name, picture, org_id FROM users "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


# ── org connections (the Configure tab) ──

CONNECTION_PROVIDERS = ("cedric-brain", "gmail", "calendar", "slack", "drive")
CONNECTION_STATUSES = ("connected", "pending", "disconnected")


def set_connection(
    org_id: str, avatar_id: str, provider: str, status: str, config: dict | None = None
) -> bool:
    """Upsert one (org, avatar, provider) connection. config is NON-SECRET
    wiring only (brain team_id/channel); credentials live in env/SSM. Unknown
    provider/status or missing ids → no-op (False)."""
    if (
        not (org_id or "").strip()
        or not (avatar_id or "").strip()
        or provider not in CONNECTION_PROVIDERS
        or status not in CONNECTION_STATUSES
    ):
        return False
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO org_connections (org_id, avatar_id, provider, status,
                                         config_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(org_id, avatar_id, provider) DO UPDATE SET
                status=excluded.status,
                config_json=excluded.config_json,
                updated_at=excluded.updated_at
            """,
            (
                org_id.strip(), avatar_id.strip(), provider, status,
                json.dumps(config or {}), time.time(),
            ),
        )
    return True


def connections_for_org(org_id: str) -> list[dict]:
    """Every connection row for an org (all avatars), config parsed."""
    if not (org_id or "").strip():
        return []
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT avatar_id, provider, status, config_json, updated_at
               FROM org_connections WHERE org_id = ?""",
            (org_id.strip(),),
        ).fetchall()
    out = []
    for r in rows:
        try:
            config = json.loads(r["config_json"]) if r["config_json"] else {}
        except ValueError:
            config = {}
        out.append(
            {
                "avatar_id": r["avatar_id"], "provider": r["provider"],
                "status": r["status"], "config": config,
                "updated_at": r["updated_at"],
            }
        )
    return out


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


def bump_speech_generation(session: Session) -> int:
    """Start a new speech turn (or cancel the current one). Returns the new
    generation; older turns' speak messages become stale everywhere."""
    with _LOCK:
        session.speech_generation += 1
        return session.speech_generation


def purge_pending_speaks(session: Session) -> None:
    """Drop queued-but-undelivered speak messages (a stop must silence the queue
    too, not just the audio already playing). Non-speak messages survive."""
    with _LOCK:
        session.pending_messages[:] = [
            m for m in session.pending_messages if m.get("type") != "speak"
        ]


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
