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

from .config import settings

# Every persisted row carries a non-null org_id (docs/infra/MULTI-TENANCY.md
# §0). Unauthenticated / service / anon rows are stamped with the fixed Demo
# tenant so the single-tenant demo stays byte-identical.
DEMO_ORG_ID = settings.demo_org_id


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
    "leave_pending",
    "integration",
}


@dataclass
class Session:
    bot_id: str
    meeting_url: str
    avatar_id: str = "laura"
    # Owning tenant (auth.py user's org). Defaults to the Demo org so a session
    # is never null-tenant; a logged-in dispatch overrides it with the user's
    # org, and legacy/unowned rows may still carry "" explicitly (the pre-auth
    # world). org_id == user_id today; the seam is what matters (MULTI-TENANCY.md).
    org_id: str = DEMO_ORG_ID
    anam_conversation_id: str = ""
    anam_conversation_url: str = ""
    transcript: list[Utterance] = field(default_factory=list)
    last_spoke_at: float = 0.0
    proactive_done: bool = False  # the one proactive flag fires at most once
    # Meter-stop retry flag: a finalized session whose Recall leave_call could
    # NOT be confirmed (auth/rate-limit/5xx/network) is kept in the store with
    # this set so the reconcile backstop retries the leave. PERSISTED — a deploy
    # restart (App Runner is ephemeral) must not drop it back to "in progress"
    # and re-strand the per-minute meter leak this flag exists to close.
    leave_pending: bool = False
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
    # Motivation-gate state (decision.should_raise_hand): raises so far, when
    # the last one happened, whether the room ignored it (timeout), and the
    # last queued point (near-dup guard). In-memory only, like the hand itself.
    hand_raise_count: int = field(default=0, repr=False, compare=False)
    hand_last_raise_at: float = field(default=0.0, repr=False, compare=False)
    hand_last_ignored: bool = field(default=False, repr=False, compare=False)
    hand_last_contribution: str = field(default="", repr=False, compare=False)
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
        """First names to EXCLUDE from the FUZZY wake match: everyone else known
        to be in the meeting, from the event roster AND transcript speakers.

        Backed by roster()'s merge (was event-roster only) so a real participant
        known only from the transcript — a "Lara" who spoke but never fired a
        join event — is still shielded and never swallowed as a corruption of
        "Laura". Broadening this exclude set can only SUPPRESS a fuzzy wake, never
        create one, so it strictly reduces false wakes. Still cheap enough for the
        partial hot path (a lowercased scan of a short transcript), and it's the
        same scan roster() already runs on the final path."""
        return self.roster()

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
        # Hot path: the per-utterance write stays on local SQLite (never a
        # network DB — latency is the product). org inherited from the session.
        _persist_utterance(self.org_id, self.bot_id, utterance)

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


def _add_column(conn: sqlite3.Connection, table: str, coldef: str) -> None:
    """Idempotent ADD COLUMN so an existing local DB upgrades in place. Mirrors
    the Postgres 0001 backfill (a new NOT NULL org_id defaults to the Demo org,
    so pre-existing rows become tenant-owned instead of null-tenant)."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
    except sqlite3.OperationalError:
        pass  # column already exists


def _init_db() -> None:
    demo = DEMO_ORG_ID
    with _LOCK, _connect() as conn:
        conn.executescript(
            f"""
            -- ── identity spine (empty now; SSO/SCIM attach later) ──
            -- No FKs into orgs on SQLite: org_id == user_id today, so real
            -- sessions carry a user id that is not (yet) a provisioned orgs
            -- row. The Postgres 0001 migration adds the FKs + RLS.
            CREATE TABLE IF NOT EXISTS orgs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                plan TEXT NOT NULL DEFAULT 'free',
                region TEXT NOT NULL DEFAULT 'eu-central-1',
                sso_connection_id TEXT,
                retention_days INTEGER NOT NULL DEFAULT 90,
                created_at REAL NOT NULL DEFAULT 0,
                deleted_at REAL
            );

            CREATE TABLE IF NOT EXISTS memberships (
                user_id TEXT NOT NULL,
                org_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',      -- owner|admin|member|billing
                status TEXT NOT NULL DEFAULT 'active',
                PRIMARY KEY (user_id, org_id)
            );

            CREATE TABLE IF NOT EXISTS org_domains (
                org_id TEXT NOT NULL,
                domain TEXT NOT NULL,                     -- NEVER map gmail.com etc.
                verified_at REAL,
                PRIMARY KEY (org_id, domain)
            );

            -- The GRANT org↔agent (a shared avatar folder becomes callable for
            -- an org). PK(org_id, avatar_id) — one grant per pair.
            CREATE TABLE IF NOT EXISTS org_agents (
                org_id TEXT NOT NULL,
                avatar_id TEXT NOT NULL,
                alias TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'org',
                status TEXT NOT NULL DEFAULT 'active',
                PRIMARY KEY (org_id, avatar_id)
            );

            -- token_hash → org_id (replaces the single global bearer). Empty
            -- now; the deps.py resolver is a later, auth-blocked PR.
            CREATE TABLE IF NOT EXISTS org_tokens (
                token_hash TEXT PRIMARY KEY,
                org_id TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0
            );

            -- append-only, METADATA ONLY, never transcript (RLS + REVOKE
            -- UPDATE/DELETE enforce append-only on Postgres; see 0001).
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id TEXT NOT NULL,
                actor_user_id TEXT,
                action TEXT NOT NULL,
                target TEXT,
                ts REAL NOT NULL DEFAULT 0
            );

            -- ── existing store tables, now org-scoped ──
            CREATE TABLE IF NOT EXISTS sessions (
                bot_id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL DEFAULT '{demo}',
                meeting_url TEXT NOT NULL,
                avatar_id TEXT NOT NULL,
                anam_conversation_id TEXT NOT NULL DEFAULT '',
                anam_conversation_url TEXT NOT NULL DEFAULT '',
                last_spoke_at REAL NOT NULL DEFAULT 0,
                proactive_done INTEGER NOT NULL DEFAULT 0,
                leave_pending INTEGER NOT NULL DEFAULT 0,
                integration_json TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS utterances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id TEXT NOT NULL DEFAULT '{demo}',
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
                org_id TEXT NOT NULL DEFAULT '{demo}',
                bot_id TEXT NOT NULL,
                FOREIGN KEY(bot_id) REFERENCES sessions(bot_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                bot_id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL DEFAULT '{demo}',
                artifact_json TEXT NOT NULL,
                saved_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_events (
                event_id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL DEFAULT '{demo}',
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
        _add_column(conn, "sessions", "integration_json TEXT NOT NULL DEFAULT ''")
        # Migration for stores created before the meter-stop retry flag. A
        # pre-existing session predates any leave failure, so 0 (not pending) is
        # the correct backfill.
        _add_column(conn, "sessions", "leave_pending INTEGER NOT NULL DEFAULT 0")
        # Migration for stores created before per-user session ownership. The
        # pre-existing column defaults to '' (legacy/unowned stays visible);
        # new sessions stamp DEMO_ORG_ID via Session.org_id.
        _add_column(conn, "sessions", "org_id TEXT NOT NULL DEFAULT ''")
        # org_id on the remaining persisted tables (idempotent; a new NOT NULL
        # column backfills existing rows to the Demo org).
        _add_column(conn, "utterances", f"org_id TEXT NOT NULL DEFAULT '{demo}'")
        _add_column(
            conn, "conversation_routes", f"org_id TEXT NOT NULL DEFAULT '{demo}'"
        )
        _add_column(conn, "artifacts", f"org_id TEXT NOT NULL DEFAULT '{demo}'")
        _add_column(conn, "scheduled_events", f"org_id TEXT NOT NULL DEFAULT '{demo}'")
        # Indexes lead with org_id; conversation_routes keeps a GLOBAL unique on
        # conversation_id (the unauthenticated ws resolves org from it alone).
        conn.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_org_bot
                ON sessions(org_id, bot_id);
            CREATE INDEX IF NOT EXISTS idx_utt_org_bot ON utterances(org_id, bot_id, id);
            CREATE INDEX IF NOT EXISTS idx_conv_routes_org ON conversation_routes(org_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_org_saved
                ON artifacts(org_id, saved_at);
            """
        )
        # Seed the Demo org row (before any backfill would need it, as on
        # Postgres). Idempotent; the demo is just an org.
        conn.execute(
            "INSERT OR IGNORE INTO orgs (id, name, slug, plan, created_at) "
            "VALUES (?, 'Demo', 'demo', 'demo', ?)",
            (demo, time.time()),
        )
    # Seed the first REAL org (outside the txn above so its own connection is a
    # clean commit; _LOCK is reentrant so nesting would be safe regardless).
    seed_builtin_orgs()


def seed_builtin_orgs() -> None:
    """Seed the first REAL tenant — SFF Studio (Swiss Founders Fund) — so the
    org_id seam is exercised for the first time: a member of an org sees only
    that org's granted agents.

    Idempotent (INSERT OR IGNORE) and safe to run on every boot — the store is
    ephemeral and re-seeds on redeploy. Gated by ``settings.seed_builtin_orgs``
    so a test can assert the un-seeded fallback.

    Only the CORPORATE domain is mapped (never a free-mail domain — org_domains
    is the trusted domain→org map; the schema comment says NEVER map gmail.com).
    Agent grants are made ONLY for avatar folders that actually exist, so a
    removed/renamed folder never leaves a dangling grant."""
    if not settings.seed_builtin_orgs:
        return
    # Lazy import keeps the module graph cycle-free: avatars never imports store
    # at load time, and store never imports avatars at load time.
    from . import avatars

    installed = set(avatars.list_ids())
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO orgs (id, name, slug, created_at) "
            "VALUES ('org_sff', 'Swiss Founders Fund', 'sff', ?)",
            (now,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO org_domains (org_id, domain, verified_at) "
            "VALUES ('org_sff', 'sffstudio.com', ?)",
            (now,),
        )
        for avatar_id in ("laura", "cedric"):
            if avatar_id in installed:
                conn.execute(
                    "INSERT OR IGNORE INTO org_agents (org_id, avatar_id) "
                    "VALUES ('org_sff', ?)",
                    (avatar_id,),
                )


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
        leave_pending=bool(row["leave_pending"]) if "leave_pending" in row.keys() else False,
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
                leave_pending, integration_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET
                meeting_url=excluded.meeting_url,
                avatar_id=excluded.avatar_id,
                org_id=excluded.org_id,
                anam_conversation_id=excluded.anam_conversation_id,
                anam_conversation_url=excluded.anam_conversation_url,
                last_spoke_at=excluded.last_spoke_at,
                proactive_done=excluded.proactive_done,
                leave_pending=excluded.leave_pending,
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
                int(session.leave_pending),
                json.dumps(session.integration) if session.integration else "",
                time.time(),
            ),
        )


def _persist_utterance(org_id: str, bot_id: str, utterance: Utterance) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO utterances (org_id, bot_id, speaker, text, ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (org_id, bot_id, utterance.speaker, utterance.text, float(utterance.ts)),
        )


def is_scheduled(event_id: str) -> bool:
    return event_id in _scheduled_events


def mark_scheduled(event_id: str, *, org_id: str = DEMO_ORG_ID) -> None:
    _scheduled_events.add(event_id)
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO scheduled_events (event_id, org_id, marked_at)
            VALUES (?, ?, ?)
            """,
            (event_id, org_id, time.time()),
        )


def save_artifact(bot_id: str, artifact: dict, *, org_id: str | None = None) -> None:
    """Persist a finished meeting's distilled artifact (never raw transcript on
    the wire; the store keeps the full copy). The row's org_id column comes from
    the explicit ``org_id`` when given, else the artifact's own ``org_id`` field
    (finalize stamps it from the session), else the Demo org — so the column and
    the artifact JSON agree and single-tenant stays byte-identical."""
    _artifacts[bot_id] = artifact
    row_org = org_id if org_id is not None else (artifact.get("org_id") or DEMO_ORG_ID)
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO artifacts (bot_id, org_id, artifact_json, saved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET
                org_id=excluded.org_id,
                artifact_json=excluded.artifact_json,
                saved_at=excluded.saved_at
            """,
            (bot_id, row_org, json.dumps(artifact), time.time()),
        )


def get_artifact(bot_id: str) -> dict | None:
    return _artifacts.get(bot_id)


def list_artifacts(org_id: str | None = None) -> list[dict]:
    """Every saved artifact with its metadata, newest first (meetings page).
    Reads the DB (not the in-memory cache) so it sees rows written by other
    processes — e.g. tests or scripts seeding the store.

    ``org_id`` seals the enumeration to one tenant (``WHERE org_id=?``); the
    default (None) returns every row for the callers that apply their own
    visibility policy downstream (dashboard.visible / /meetings/list scope on
    the artifact's own org_id, which keeps the legacy ""=shared semantics)."""
    with _LOCK, _connect() as conn:
        if org_id is None:
            rows = conn.execute(
                "SELECT bot_id, org_id, artifact_json, saved_at FROM artifacts "
                "ORDER BY saved_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT bot_id, org_id, artifact_json, saved_at FROM artifacts "
                "WHERE org_id=? ORDER BY saved_at DESC",
                (org_id,),
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
    bot_id: str, meeting_url: str, avatar_id: str = "laura", org_id: str = DEMO_ORG_ID
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


def org_id_for_email(email: str) -> str:
    """The org a login resolves to. A VERIFIED corporate domain (a row in
    org_domains with a non-null verified_at) maps every login at that domain to
    the shared org; everything else falls back to the personal org
    (org_id == user_id today). org_domains never contains a free-mail domain
    (the seed enforces that), so gmail/outlook logins always stay personal."""
    normalized = (email or "").strip().lower()
    _, _, domain = normalized.partition("@")
    if domain:
        with _LOCK, _connect() as conn:
            row = conn.execute(
                "SELECT org_id FROM org_domains "
                "WHERE domain = ? AND verified_at IS NOT NULL",
                (domain,),
            ).fetchone()
        if row:
            return row["org_id"]
    return user_id_for_email(email)


def upsert_user(email: str, name: str = "", picture: str = "") -> dict:
    """Create-or-refresh a user row at login. Returns the user dict + created.

    org_id resolves via org_id_for_email: a verified corporate domain maps to
    its shared org (and an active membership row is created), everything else
    keeps the personal-org invariant org_id == user_id (backward-compatible)."""
    email = (email or "").strip().lower()
    uid = user_id_for_email(email)
    org_id = org_id_for_email(email)
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
            -- org_id is refreshed so a domain verified AFTER a user's first
            -- login takes effect on their next one. Safe on THIS ephemeral
            -- SQLite store (wiped + re-seeded every boot, so resolution is
            -- deterministic from a user's first login of the boot and there is
            -- no persisted pre-seed history to orphan). PORTABILITY NOTE for the
            -- Postgres control-plane: there, reassigning a returning user's org
            -- must be paired with a backfill of their prior artifacts/sessions
            -- org_id, else old rows fall outside the new org's visibility.
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                picture=excluded.picture,
                org_id=excluded.org_id,
                last_login_at=excluded.last_login_at
            """,
            (uid, email, name, picture, org_id, now, now),
        )
        # A real org (resolved org differs from the personal uid) gets an
        # explicit membership row so the org↔user link exists for roles /
        # governance. Personal orgs (org_id == user_id) need no membership.
        if org_id != uid:
            conn.execute(
                "INSERT OR IGNORE INTO memberships "
                "(user_id, org_id, role, status) VALUES (?, ?, 'member', 'active')",
                (uid, org_id),
            )
    return {"user_id": uid, "email": email, "name": name, "picture": picture,
            "org_id": org_id, "created": created}


def list_org_agent_ids(org_id: str) -> list[str]:
    """The avatar_ids granted to an org (active org_agents rows), sorted. Empty
    when the org has no grants — the caller (avatars.list_for_org) then falls
    back to the full avatar list, so personal/demo/unknown orgs stay
    all-avatars (backward-compatible)."""
    if not (org_id or "").strip():
        return []
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT avatar_id FROM org_agents "
            "WHERE org_id = ? AND status = 'active' ORDER BY avatar_id",
            (org_id.strip(),),
        ).fetchall()
    return [r["avatar_id"] for r in rows]


def get_user(user_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT user_id, email, name, picture, org_id FROM users "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def org_exists(org_id: str) -> bool:
    """Whether ``org_id`` is a provisioned org row. A SHARED org (e.g. org_sff,
    resolved from a verified domain) lives in ``orgs`` and is NEVER a ``users``
    row — so callers validating an org must not use ``get_user`` alone, which
    only matches personal orgs where org_id == user_id."""
    if not (org_id or "").strip():
        return False
    with _LOCK, _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM orgs WHERE id = ? AND deleted_at IS NULL",
            (org_id.strip(),),
        ).fetchone() is not None


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


def register_conversation(
    conversation_id: str, bot_id: str, *, org_id: str = DEMO_ORG_ID
) -> None:
    _by_conversation[conversation_id] = bot_id
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO conversation_routes (conversation_id, org_id, bot_id)
            VALUES (?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                org_id=excluded.org_id, bot_id=excluded.bot_id
            """,
            (conversation_id, org_id, bot_id),
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
