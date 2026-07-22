"""Persistent session store + transient websocket registry.

Session state survives process restarts through SQLite. Live WebSocket objects
are intentionally kept in memory only; avatar pages reconnect and re-register
against the persisted conversation routing state.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from ..config import settings

# Every persisted row carries a non-null org_id (docs/infra/MULTI-TENANCY.md
# §0). Unauthenticated / service / anon rows are stamped with the fixed Demo
# tenant so the single-tenant demo stays byte-identical.
DEMO_ORG_ID = settings.demo_org_id


def _default_store_path() -> Path:
    persistent_mount = Path("/var/data")
    if persistent_mount.exists() and os.access(persistent_mount, os.W_OK):
        return persistent_mount / "laura-store.sqlite3"
    return Path(__file__).resolve().parents[2] / "data" / "store.sqlite3"


STORE_PATH = Path(os.getenv("LAURA_STORE_PATH", str(_default_store_path())))


@dataclass
class Utterance:
    # participant_id is the stable internal identity. speaker remains the
    # presentation label only; two people may legitimately share it.
    speaker: str
    text: str
    ts: float
    participant_id: str = ""
    speaker_kind: str = "human"  # human | agent


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
    # WHO dispatched the session (dashboard user_id), "" for service starts.
    # In-memory only (not a persisted field): a mid-meeting restart loses it
    # and the artifact falls back to transcript-attendance scoping.
    principal_id: str = ""
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
    # One-shot flag: the self-introduction on join (main.maybe_self_introduce)
    # is scheduled at most once per session. Set BEFORE the delayed task launches
    # so racing transcript webhooks can't double-schedule it. In-memory only (a
    # mid-meeting restart re-introducing once is harmless, and it self-suppresses
    # if she has spoken since).
    self_introduced: bool = field(default=False, repr=False, compare=False)
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
    # One-shot flags for the spoken usage-deadline warnings (~5 min / ~1 min
    # before the entitlement runs out — main._usage_warn). In-memory only: a
    # mid-meeting restart repeating one warning is harmless, and the durable
    # deadline itself lives in Postgres (entitlements.usage_sessions).
    usage_warned_5m: bool = field(default=False, repr=False, compare=False)
    usage_warned_1m: bool = field(default=False, repr=False, compare=False)
    # Deferred usage-close context (PR B BLOCKER 2): when finalize could NOT
    # confirm the Recall meter stopped (leave_pending), the usage row is left
    # OPEN so the slot stays held until the meter is verified off — these carry
    # the close reason + Recall end timestamp to the eventual _retry_leave that
    # confirms the stop and closes the row. In-memory only (a restart re-derives
    # a safe default: reason 'ended', consumed capped at the deadline).
    usage_close_reason: str = field(default="", repr=False, compare=False)
    usage_end_epoch: Any = field(default=None, repr=False, compare=False)
    _persist_enabled: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_persist_enabled", True)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        if name in _PERSISTED_SESSION_FIELDS and getattr(
            self, "_persist_enabled", False
        ):
            _persist_session(self)

    @staticmethod
    def _explicit_agent_metadata(participant: dict | None) -> bool:
        """Conservative Recall participant classifier.

        Names are NEVER identity. We accept only explicit bot/agent metadata,
        a bot_id binding to this Session, or participant_id == bot_id. If the
        provider omits all of those signals we deliberately classify as human
        rather than guessing from a display name.
        """
        if not isinstance(participant, dict):
            return False
        containers = [participant]
        for key in ("metadata", "extra_data"):
            value = participant.get(key)
            if isinstance(value, dict):
                containers.append(value)
        for value in containers:
            if value.get("is_bot") is True or value.get("is_agent") is True:
                return True
            role = str(value.get("kind") or value.get("type") or value.get("role") or "").lower()
            if role in {"bot", "agent", "meeting_bot"}:
                return True
        return False

    def resolve_participant(
        self,
        name: str | None,
        participant_id: Any = None,
        *,
        metadata: dict | None = None,
    ) -> dict[str, str]:
        """Resolve a canonical participant, keyed by Recall participant_id.

        A later transcript without a name reuses the name learned from the
        participant-event map. The returned display name is presentation only.
        """
        key = "" if participant_id is None else str(participant_id)
        supplied_name = (name or "").strip()
        explicit_agent = (
            key == self.bot_id
            or str((metadata or {}).get("bot_id") or "") == self.bot_id
            or self._explicit_agent_metadata(metadata)
        )
        if not key:
            # An explicit bot event without a provider participant_id must not
            # poison a same-name human's legacy identity. Bind it to this
            # session bot; never infer that later name-only lines are the bot.
            key = (
                f"agent:{self.bot_id}"
                if explicit_agent
                else (
                    f"legacy:{supplied_name.lower()}"
                    if supplied_name
                    else "legacy:guest"
                )
            )

        existing = self.participants.get(key) or {}
        display_name = supplied_name or str(existing.get("name") or "").strip()
        if not display_name:
            label = self._anon_labels.get(key)
            if label is None:
                label = f"Guest {len(self._anon_labels) + 1}"
                self._anon_labels[key] = label
            display_name = label

        kind = "agent" if explicit_agent else str(existing.get("kind") or "human")
        here = bool(existing.get("here", True))
        identity = {"id": key, "name": display_name, "kind": kind, "here": here}
        changed = (
            not existing
            or str(existing.get("name") or "") != display_name
            or str(existing.get("kind") or "human") != kind
        )
        self.participants[key] = identity
        if changed:
            _persist_participant(self.org_id, self.bot_id, identity)
        return identity

    def resolve_speaker(self, name: str | None, participant_id: Any = None) -> str:
        """Backward-compatible presentation label; use resolve_participant for logic."""
        return self.resolve_participant(name, participant_id)["name"]

    def participant_event(
        self,
        name: str | None,
        participant_id: Any,
        *,
        here: bool,
        metadata: dict | None = None,
    ) -> dict[str, str]:
        """Fold a Recall join/leave into the canonical, persisted roster."""
        identity = self.resolve_participant(name, participant_id, metadata=metadata)
        identity["here"] = bool(here)
        self.participants[identity["id"]] = identity
        _persist_participant(self.org_id, self.bot_id, identity)
        return identity

    def present_names(self, avatar_name: str = "") -> list[str]:
        """First names to EXCLUDE from the FUZZY wake match: everyone else known
        to be in the meeting, from the event roster AND transcript speakers.

        Backed by roster()'s merge (was event-roster only) so a real participant
        known only from the transcript — a "Lara" who spoke but never fired a
        join event — is still shielded and never swallowed as a corruption of
        "Laura". Broadening this exclude set can only SUPPRESS a fuzzy wake, never
        create one, so it strictly reduces false wakes. Still cheap enough for the
        partial hot path (a lowercased scan of a short transcript), and it's the
        same scan roster() already runs on the final path.

        ``avatar_name`` (the running avatar's own name) is forwarded to roster()
        so its "everyone besides the avatar itself" contract stays literally true
        — the avatar's own name is never in the exclude set that would shield a
        fuzzy corruption of its wake word."""
        return self.roster(avatar_name)

    def roster(self, avatar_name: str = "") -> list[str]:
        """Human roster, keyed internally by participant_id.

        avatar_name is retained for API compatibility but is intentionally
        not used for classification: a human may share the avatar's display
        name. Agent rows are filtered by persisted kind/id instead.
        """
        names: list[str] = []
        seen_ids: set[str] = set()
        gone_ids = {
            str(pid)
            for pid, p in self.participants.items()
            if not p.get("here", True)
        }
        for pid, p in self.participants.items():
            if p.get("here", True) and p.get("kind", "human") != "agent":
                names.append(str(p.get("name") or "Guest"))
                seen_ids.add(str(pid))
        for u in self.transcript:
            pid = u.participant_id or f"legacy:{u.speaker.lower()}"
            if (
                u.speaker_kind != "agent"
                and pid not in seen_ids
                and pid not in gone_ids
            ):
                names.append(u.speaker)
                seen_ids.add(pid)
        return names

    def add_utterance(
        self,
        speaker: str,
        text: str,
        *,
        participant_id: str = "",
        speaker_kind: str = "human",
    ) -> None:
        resolved_id = str(participant_id or "")
        resolved_kind = speaker_kind
        if not resolved_id:
            # Backward-compatible direct callers: bind by name only when the
            # live participant map has exactly one unambiguous match.
            matches = [
                (str(pid), p)
                for pid, p in self.participants.items()
                if str(p.get("name") or "").strip().lower()
                == speaker.strip().lower()
            ]
            if len(matches) == 1:
                resolved_id, participant = matches[0]
                resolved_kind = str(participant.get("kind") or speaker_kind)
        utterance = Utterance(
            speaker=speaker,
            text=text,
            ts=time.time(),
            participant_id=resolved_id,
            speaker_kind="agent" if resolved_kind == "agent" else "human",
        )
        self.transcript.append(utterance)
        # Hot path: the per-utterance write stays on local SQLite (never a
        # network DB — latency is the product). org inherited from the session.
        # Honors _persist_enabled like the field writes: an ephemeral session
        # (unit tests flip it off) has no sessions row for the utterances FK.
        if getattr(self, "_persist_enabled", False):
            _persist_utterance(self.org_id, self.bot_id, utterance)

    def transcript_text(self, *, include_agents: bool = True) -> str:
        utterances = self.transcript if include_agents else self.human_transcript()
        return "\n".join(f"{u.speaker}: {u.text}" for u in utterances)

    def human_transcript(self) -> list[Utterance]:
        """Human-only evidence for every live and post-meeting decision path."""
        return [u for u in self.transcript if u.speaker_kind != "agent"]

    def recent_transcript(self, n: int = 8) -> str:
        """Recent HUMAN turns; agent output is never evidence for a new answer."""
        return "\n".join(
            f"{u.speaker}: {u.text}" for u in self.human_transcript()[-n:]
        )

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

            -- Canonical approval record (agreed action-lifecycle contract,
            -- handshake operation approve-action): ONE decision per action,
            -- all channels (dashboard/Slack relay) converge here. Replays are
            -- answered from this row; a conflicting decision is a 409. Never
            -- transcript content — decisions + distilled refs only.
            CREATE TABLE IF NOT EXISTS action_approvals (
                org_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                decision TEXT NOT NULL,                -- approve|reject|respond
                selected_slot_id TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL DEFAULT '',
                decided_via TEXT NOT NULL DEFAULT '',  -- dashboard|slack
                laura_user_id TEXT NOT NULL DEFAULT '',
                previous_status TEXT NOT NULL DEFAULT '',
                new_status TEXT NOT NULL DEFAULT '',
                execution_job_id TEXT,
                blocked_on TEXT NOT NULL DEFAULT '',   -- JSON array of action_ids
                decided_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (org_id, action_id)
            );

            -- Dashboard chat channel (org ↔ Cedric, replacing Slack as the
            -- approval surface). One row per message; `kind` separates plain
            -- text from action cards (which reference action_id and render
            -- approve/reject inline — the DECISION still lives only in
            -- action_approvals above; chat never stores decisions). Distilled
            -- content only, never transcript text.
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id TEXT NOT NULL,
                sender TEXT NOT NULL,                  -- user|cedric|system
                sender_label TEXT NOT NULL DEFAULT '', -- display name
                kind TEXT NOT NULL DEFAULT 'text',     -- text|action_card
                body TEXT NOT NULL DEFAULT '',
                action_id TEXT NOT NULL DEFAULT '',    -- set on action cards
                payload_json TEXT NOT NULL DEFAULT '', -- card fields: item/owner/due
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chat_messages_org
                ON chat_messages(org_id, id);

            -- Edited typed params for a captured action (canonical Action
            -- plane, key-free mode). The artifact stays immutable; this
            -- overlay is what the approve doors execute. Durable orgs use
            -- queued_actions.typed_json in Postgres instead.
            CREATE TABLE IF NOT EXISTS action_typed_overrides (
                org_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                typed_json TEXT NOT NULL,
                updated_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (org_id, action_id)
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
                participant_id TEXT NOT NULL DEFAULT '',
                speaker_kind TEXT NOT NULL DEFAULT 'human',
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

            -- Raw capabilities never enter SQLite. A random URL capability is
            -- hashed and bound to exactly one Recall bot before realtime
            -- transcript events are accepted.
            CREATE TABLE IF NOT EXISTS recall_realtime_capabilities (
                capability_hash TEXT PRIMARY KEY,
                bot_id TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                FOREIGN KEY(bot_id) REFERENCES sessions(bot_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS session_participants (
                bot_id TEXT NOT NULL,
                org_id TEXT NOT NULL DEFAULT '{demo}',
                participant_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                speaker_kind TEXT NOT NULL DEFAULT 'human',
                here INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL,
                PRIMARY KEY (bot_id, participant_id),
                FOREIGN KEY(bot_id) REFERENCES sessions(bot_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                bot_id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL DEFAULT '{demo}',
                artifact_json TEXT NOT NULL,
                saved_at REAL NOT NULL
            );

            -- Per-avatar brain choice (dashboard toggle): "gemini" (tutto-Gemini
            -- via the ears relay) or "cerebras" (the normal Deepgram+brain path).
            -- Read at bot-start and in the webhook; changes take effect on the
            -- NEXT meeting with no redeploy. Absent row = the global default
            -- (settings.gemini_ears_mode).
            CREATE TABLE IF NOT EXISTS avatar_brain_mode (
                avatar_id TEXT PRIMARY KEY,
                brain_mode TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            -- Small per-org preference switches (dashboard toggles), one row per
            -- (org_id, key). First key: "show_transcripts" — whether the meeting
            -- view displays the stored transcript pane (default OFF: absent row).
            -- Values are short strings ("1"/"0"); never store content here.
            CREATE TABLE IF NOT EXISTS org_prefs (
                org_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (org_id, key)
            );

            -- Per-avatar capability switches (dashboard toggles). One row per
            -- (avatar, capability) — "google" | "slack" — so each avatar can
            -- independently turn ON/OFF a capability the ORG connected once in
            -- the Connections view. Absent row = unset → the caller applies the
            -- default (ON when the org has that integration connected, else off).
            -- Read at the execute/deliver seams; keyed by the avatar_id string
            -- ONLY (no org, no ::uuid → split-brain safe). Litestream-replicated
            -- like avatar_brain_mode.
            CREATE TABLE IF NOT EXISTS avatar_capabilities (
                avatar_id TEXT NOT NULL,
                capability TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (avatar_id, capability)
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
                last_login_at REAL NOT NULL,
                -- Durable control-plane user UUID (from control_plane.ensure_user)
                -- when configured; '' otherwise. The billing member-role lookup
                -- keys on this UUID, NOT the ephemeral u_<hash> cache-key user_id.
                member_uid TEXT NOT NULL DEFAULT ''
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
            -- Per-org OAuth refresh token for the NATIVE Google executor
            -- (docs/product/NATIVE-INTEGRATIONS-PLAN.md). The token is stored
            -- ENCRYPTED (crypto.encrypt), never in plaintext; scopes records
            -- what the user consented to so the executor can fail soft when a
            -- write scope is missing.
            CREATE TABLE IF NOT EXISTS org_oauth (
                org_id TEXT NOT NULL,
                provider TEXT NOT NULL,      -- 'google'
                refresh_token_enc TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                scopes TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY (org_id, provider)
            );
            -- Per-USER OAuth refresh token for the personal-calendar VIEW
            -- (/dashboard/upcoming). org_oauth above is keyed by ORG, so in a
            -- SHARED org (a verified corporate domain maps every colleague to one
            -- org_id) a single Google token would be read back by every member —
            -- one person's calendar leaking to the whole domain. This table keys
            -- the token to the connecting HUMAN so each member sees only their own
            -- calendar. Same encryption + shape as org_oauth. Created fresh here
            -- on every boot (CREATE IF NOT EXISTS) so a litestream-restored store
            -- gains the table without a data migration.
            CREATE TABLE IF NOT EXISTS user_oauth (
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,      -- 'google'
                refresh_token_enc TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                scopes TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, provider)
            );

            -- First-class Decision records (mirror of the durable
            -- meeting_decisions table, migration 0017). A decision gets its
            -- own identity, maker, reason, related_project and supersede link
            -- so the archive can show "this decision supersedes the one from
            -- July 15". source_ref holds a bot_id/meeting_key ONLY — never
            -- transcript text (PII). status ∈ active|superseded|revisited; a
            -- superseded decision keeps its row (history), never deleted.
            -- Self-created at boot so a litestream-restored store gains it
            -- without a data migration. Durable orgs route to Postgres.
            CREATE TABLE IF NOT EXISTS meeting_decisions (
                org_id TEXT NOT NULL DEFAULT '{demo}',
                id TEXT NOT NULL,
                bot_id TEXT NOT NULL DEFAULT '',
                decision TEXT NOT NULL,
                decision_maker TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                related_project TEXT NOT NULL DEFAULT '',
                supersedes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                source_ref TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (org_id, id)
            );
            """
        )
        # Migration for stores created before the Cedric integration column.
        _add_column(conn, "users", "member_uid TEXT NOT NULL DEFAULT ''")
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
        _add_column(conn, "utterances", "participant_id TEXT NOT NULL DEFAULT ''")
        _add_column(conn, "utterances", "speaker_kind TEXT NOT NULL DEFAULT 'human'")
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
            CREATE INDEX IF NOT EXISTS idx_participants_org_bot
                ON session_participants(org_id, bot_id);
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
    from .. import avatars

    # Is the durable control plane configured? Checked DIRECTLY off settings —
    # NOT via control_plane.enabled() — ON PURPOSE: seed_builtin_orgs runs
    # during store's OWN module init (_init_db at import tail), and
    # control_plane imports names from store at its module top, so importing
    # control_plane here creates a circular import that crashes with
    # "partially initialized module" whenever control_plane is imported before
    # store. This one line mirrors control_plane.enabled() exactly; keep them
    # in sync.
    control_plane_on = bool((settings.laura_database_url or "").strip())
    if control_plane_on:
        # Durable identity owns the domain→org mapping in production
        # (laura_private.ensure_user + verified Postgres org_domains rows).
        # The SQLite seed would mint a non-uuid shadow tenant ('org_sff') that
        # no durable path can serve — billing/entitlements/outbox all cast
        # org_id to uuid — and that sent a whole diagnosis down the wrong
        # trail (2026-07-15). Skip the seed and sweep any previously seeded
        # rows instead: idempotent, runs on every boot (init() → here) AFTER
        # the Litestream restore, so an old replica self-heals. Data rows
        # (sessions/artifacts/users/…) were verified to never carry org_sff;
        # the sweep deliberately touches only the three seeded tables.
        with _LOCK, _connect() as conn:
            conn.execute("DELETE FROM org_domains WHERE org_id = 'org_sff'")
            conn.execute("DELETE FROM org_agents WHERE org_id = 'org_sff'")
            conn.execute("DELETE FROM orgs WHERE id = 'org_sff'")
        return

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
            "SELECT bot_id, speaker, participant_id, speaker_kind, text, ts "
            "FROM utterances ORDER BY id"
        ).fetchall()
        participant_rows = conn.execute(
            "SELECT bot_id, participant_id, display_name, speaker_kind, here "
            "FROM session_participants"
        ).fetchall()
        routes = conn.execute(
            "SELECT conversation_id, bot_id FROM conversation_routes"
        ).fetchall()
        artifacts = conn.execute("SELECT bot_id, artifact_json FROM artifacts").fetchall()
        scheduled = conn.execute("SELECT event_id FROM scheduled_events").fetchall()

    utterances_by_bot: dict[str, list[Utterance]] = {}
    for row in utterance_rows:
        utterances_by_bot.setdefault(row["bot_id"], []).append(
            Utterance(
                speaker=row["speaker"],
                text=row["text"],
                ts=float(row["ts"]),
                participant_id=row["participant_id"],
                speaker_kind=row["speaker_kind"],
            )
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
    for row in participant_rows:
        session = _sessions.get(row["bot_id"])
        if session is not None:
            session.participants[row["participant_id"]] = {
                "id": row["participant_id"],
                "name": row["display_name"],
                "kind": row["speaker_kind"],
                "here": bool(row["here"]),
            }
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
            INSERT INTO utterances (
                org_id, bot_id, speaker, participant_id, speaker_kind, text, ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                org_id,
                bot_id,
                utterance.speaker,
                utterance.participant_id,
                utterance.speaker_kind,
                utterance.text,
                float(utterance.ts),
            ),
        )


def _persist_participant(org_id: str, bot_id: str, identity: dict) -> None:
    """Persist identity/classification so restart uses the same decisions."""
    with _LOCK, _connect() as conn:
        # Unit-level Session objects may not be store-backed. Production
        # sessions are persisted before participant events arrive.
        if conn.execute(
            "SELECT 1 FROM sessions WHERE bot_id=?", (bot_id,)
        ).fetchone() is None:
            return
        conn.execute(
            """
            INSERT INTO session_participants (
                bot_id, org_id, participant_id, display_name,
                speaker_kind, here, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot_id, participant_id) DO UPDATE SET
                display_name=excluded.display_name,
                speaker_kind=excluded.speaker_kind,
                here=excluded.here,
                updated_at=excluded.updated_at
            """,
            (
                bot_id,
                org_id,
                identity["id"],
                identity["name"],
                identity.get("kind", "human"),
                int(bool(identity.get("here", True))),
                time.time(),
            ),
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



def durable_artifacts_enabled() -> bool:
    """Whether Postgres is actually configured as the artifact authority.

    Some key-free identity tests replace control_plane.enabled while leaving
    LAURA_DATABASE_URL empty. That simulates an OAuth cutover, not a reachable
    archive database; artifact routing must remain SQLite in that state.
    """
    from . import control_plane

    return control_plane.enabled() and bool(
        (settings.laura_database_url or "").strip()
    )


def save_artifact(bot_id: str, artifact: dict, *, org_id: str | None = None) -> None:
    """Persist a finished meeting's complete private artifact.

    In production Postgres is the source of truth and is written FIRST. A
    configured database outage therefore propagates before the local cache is
    mutated, leaving finalize retryable instead of reporting a false success.
    SQLite remains the key-free/demo store and the live utterance hot path.
    """
    row_org = org_id if org_id is not None else (artifact.get("org_id") or DEMO_ORG_ID)
    saved_at = time.time()

    # Lazy import avoids the control_plane -> store constants import cycle.
    from . import control_plane

    # A session-shaped personal org (u_<hash>) has no durable archive and would
    # crash the org_id uuid cast (same degrade as list_artifacts) — SQLite
    # remains its complete persistence path, so save errors stay fatal for it.
    durable_enabled = durable_artifacts_enabled() and control_plane.is_durable_org(
        str(row_org)
    )
    if durable_enabled:
        control_plane.save_artifact(
            row_org,
            bot_id,
            artifact,
            visibility=str(artifact.get("visibility") or "participants"),
            saved_at=saved_at,
        )

    # The local copy is a same-process warm cache in production and remains the
    # complete persistence path for key-free/demo deployments. Once Postgres
    # committed, a broken ephemeral SQLite mirror must not turn durable success
    # into an endless finalize retry; without Postgres, the same error remains
    # fatal so the key-free demo never reports a false save.
    _artifacts[bot_id] = artifact
    try:
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
                (bot_id, row_org, json.dumps(artifact), saved_at),
            )
    except Exception:
        if not durable_enabled:
            _artifacts.pop(bot_id, None)
            raise

    # First-class decision records (0017): persist alongside the artifact, with
    # supersede linking. Best-effort + idempotent (skip if this meeting already
    # has decision rows) so a finalize retry never double-inserts and a decision
    # write failure never turns a saved artifact into a finalize loop. Never
    # logs decision text.
    records = artifact.get("decision_records")
    if isinstance(records, list) and records:
        try:
            if not list_decisions(str(row_org), bot_id):
                persist_decision_records(str(row_org), bot_id, records)
        except Exception as exc:  # noqa: BLE001 — decision persistence is non-fatal
            print(
                f"[save_artifact] decision persistence skipped "
                f"({type(exc).__name__})",
                flush=True,
            )


def get_artifact(bot_id: str, org_id: str | None = None) -> dict | None:
    """Get a private artifact.

    A production durable lookup is permitted only when its org is already
    trusted. A bot-only call deliberately remains local/same-process: resolving
    an org from a caller-controlled bot_id would require a forbidden global
    artifact lookup. Key-free SQLite behavior is unchanged.
    """
    if org_id is not None:
        from . import control_plane

        # Personal (u_<hash>) orgs degrade to the SQLite warm cache below —
        # the durable lookup's uuid cast would 500 (same rule as list_artifacts).
        if durable_artifacts_enabled() and control_plane.is_durable_org(
            str(org_id)
        ):
            return control_plane.get_artifact(org_id, bot_id)
    artifact = _artifacts.get(bot_id)
    if artifact is not None and org_id is not None:
        artifact_org = str(artifact.get("org_id") or "")
        if artifact_org not in ("", str(org_id)):
            return None
    return artifact


def list_artifacts(org_id: str | None = None) -> list[dict]:
    """Every saved artifact with its metadata, newest first.

    Production enumeration always requires a trusted org and is served from
    Postgres under transaction-local RLS. The optional global SQLite path is
    retained only for the key-free demo and legacy callers that apply their own
    local visibility policy.
    """
    from . import control_plane

    if durable_artifacts_enabled():
        if not (org_id or "").strip():
            raise ValueError(
                "org_id is required for production artifact enumeration"
            )
        # A session-shaped personal org (u_<hash>) has no durable archive and
        # would crash the WHERE org_id = CAST(:o AS uuid) / RLS uuid cast.
        # Degrade to the SQLite warm cache instead of a 500.
        if control_plane.is_durable_org(str(org_id)):
            return control_plane.list_artifacts(str(org_id)) or []

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
    for row in rows:
        try:
            saved = json.loads(row["artifact_json"])
        except (TypeError, ValueError):
            saved = {}
        out.append(
            {
                "bot_id": row["bot_id"],
                "saved_at": row["saved_at"],
                "artifact": saved,
            }
        )
    return out

def create(
    bot_id: str, meeting_url: str, avatar_id: str = "laura", org_id: str = DEMO_ORG_ID,
    principal_id: str = "",
) -> Session:
    s = Session(
        bot_id=bot_id, meeting_url=meeting_url, avatar_id=avatar_id, org_id=org_id,
        principal_id=principal_id,
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
    (the seed enforces that), so gmail/outlook logins always stay personal.

    DURABLE deployments: this is only a pre-override hint — upsert_user replaces
    the result with control_plane.ensure_user()'s uuid org (the identity source
    of truth; verified Postgres org_domains rows do the domain mapping there).
    Don't diagnose prod org resolution from this function alone."""
    normalized = (email or "").strip().lower()
    _, _, domain = normalized.partition("@")
    # PERSONAL-FIRST (owner decision): domain→shared-org routing only runs
    # when explicitly enabled. Default off ⇒ every login — verified corporate
    # domain included — resolves to its own personal org. The org_domains
    # rows stay in place (the parked "teams" feature flips this back on).
    if domain and settings.shared_domain_orgs:
        with _LOCK, _connect() as conn:
            row = conn.execute(
                "SELECT org_id FROM org_domains "
                "WHERE domain = ? AND verified_at IS NOT NULL",
                (domain,),
            ).fetchone()
        if row:
            return row["org_id"]
    return user_id_for_email(email)


def upsert_user(
    email: str, name: str = "", picture: str = "", google_sub: str = ""
) -> dict:
    """Create-or-refresh a user row at login. Returns the user dict + created.

    org_id resolves via org_id_for_email: a verified corporate domain maps to
    its shared org (and an active membership row is created), everything else
    keeps the personal-org invariant org_id == user_id (backward-compatible).

    DURABLE control plane (LAURA_DATABASE_URL set): the Postgres signup
    (control_plane.ensure_user) is the identity source of truth — its UUID
    org wins and is stored on this row, while user_id stays the email-derived
    ``u_<hash>`` (the cookie cache key; this SQLite row is ephemeral and the
    cookie must land on the same user after a redeploy). ``google_sub`` is the
    Google OIDC subject from auth.py's verified claims; empty for direct
    test/tool callers. Control plane off → exactly today's behavior."""
    email = (email or "").strip().lower()
    uid = user_id_for_email(email)
    org_id = org_id_for_email(email)
    member_uid = ""  # durable Postgres user UUID; set below when configured
    from . import control_plane  # lazy: control_plane imports store at load

    if control_plane.enabled():
        try:
            durable = control_plane.ensure_user(google_sub, email, name, picture)
        except Exception as exc:  # noqa: BLE001 — convert to a PII-safe failure
            # Fail closed: a local u_<hash> fallback is not a durable UUID org
            # and could create a parallel tenant. Do not let the original
            # SQLAlchemy exception (which may render bound email/sub values)
            # escape into logs.
            print(
                "[control_plane] ensure_user failed "
                f"({type(exc).__name__}); rejecting login",
                flush=True,
            )
            raise RuntimeError("durable identity is temporarily unavailable") from None
        if not durable:
            raise RuntimeError("durable identity is temporarily unavailable")
        org_id = durable["org_id"]
        member_uid = str(durable.get("user_id") or "")
    now = time.time()
    with _LOCK, _connect() as conn:
        prev = conn.execute(
            "SELECT org_id FROM users WHERE user_id = ?", (uid,)
        ).fetchone()
        created = prev is None
        conn.execute(
            """
            INSERT INTO users (user_id, email, name, picture, org_id,
                               created_at, last_login_at, member_uid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            -- org_id is refreshed so a domain verified AFTER a user's first
            -- login takes effect on their next one. Safe on THIS ephemeral
            -- SQLite store (wiped + re-seeded every boot, so resolution is
            -- deterministic from a user's first login of the boot and there is
            -- no persisted pre-seed history to orphan). The CONTROL-PLANE
            -- cutover (u_<hash> -> durable UUID org) is the one reassignment
            -- with prior rows to keep: _restamp_personal_org below moves them.
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                picture=excluded.picture,
                org_id=excluded.org_id,
                last_login_at=excluded.last_login_at,
                member_uid=excluded.member_uid
            """,
            (uid, email, name, picture, org_id, now, now, member_uid),
        )
        # Cutover backfill (adversarial review 2026-07-13, blocker 2): when
        # the resolved org CHANGES from the row's previous one AND that
        # previous org was the user's own personal u_<hash> org, re-stamp
        # their existing rows in the SAME transaction — otherwise the moment
        # the control plane flips on, a returning user's history (artifacts/
        # sessions/ledger stamped u_<hash>) silently falls outside the new
        # org's visibility set. Idempotent and one-time per user: after this
        # login the users row carries the new org, so the condition is False.
        # A previous SHARED org (verified-domain, e.g. org_sff) is never
        # touched — those rows belong to the org, not the person.
        if prev is not None and prev["org_id"] == uid and org_id != uid:
            _restamp_personal_org(conn, uid, org_id)
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
            "org_id": org_id, "member_uid": member_uid, "created": created}


def _restamp_personal_org(conn: sqlite3.Connection, old_org: str, new_org: str) -> None:
    """Move every row owned by a user's PERSONAL org (org_id == u_<hash>) to
    their new durable org — the control-plane cutover backfill (see the call
    site in upsert_user). Runs on the caller's connection/transaction.

    Artifacts need BOTH the column and the JSON's own org_id field re-stamped:
    /meetings/list and dashboard._meeting_row scope on the artifact JSON, so a
    column-only update would leave the history invisible anyway. In-memory
    caches (_artifacts, _sessions) are synced so the change is visible without
    a restart; live Session objects are updated via object.__setattr__ (the
    row is already written here — no need to re-trigger per-field persistence).
    """
    rows = conn.execute(
        "SELECT bot_id, artifact_json FROM artifacts WHERE org_id = ?", (old_org,)
    ).fetchall()
    for r in rows:
        try:
            artifact = json.loads(r["artifact_json"]) if r["artifact_json"] else {}
        except ValueError:
            artifact = {}
        artifact["org_id"] = new_org
        conn.execute(
            "UPDATE artifacts SET org_id = ?, artifact_json = ? WHERE bot_id = ?",
            (new_org, json.dumps(artifact), r["bot_id"]),
        )
        if r["bot_id"] in _artifacts:
            _artifacts[r["bot_id"]]["org_id"] = new_org
    conn.execute(
        "UPDATE sessions SET org_id = ? WHERE org_id = ?", (new_org, old_org)
    )
    for s in _sessions.values():
        if s.org_id == old_org:
            object.__setattr__(s, "org_id", new_org)
    try:
        # ledger_items shares the sqlite file but is owned by ledger.py — its
        # table may not exist in store-only unit contexts; best-effort.
        conn.execute(
            "UPDATE ledger_items SET org_id = ? WHERE org_id = ?",
            (new_org, old_org),
        )
    except sqlite3.OperationalError:
        pass


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
            "SELECT user_id, email, name, picture, org_id, member_uid FROM users "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def resolve_org_token(raw_token: str) -> str | None:
    """org_id owning this raw machine bearer, or None. Same contract as
    control_plane.resolve_org_token (sha256(raw) looked up in org_tokens) —
    the SQLite fallback so per-org service starts work before/without the
    Postgres control plane. Never logs the token."""
    import hashlib

    raw = (raw_token or "").strip()
    if not raw:
        return None
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT org_id FROM org_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
    return row["org_id"] if row else None


def mint_org_token(org_id: str, label: str = "") -> str | None:
    """Mint a per-org machine bearer in the SQLite org_tokens table: store
    sha256(raw), return the raw ONCE (provisioning / tests). None on empty
    org_id. NOTE: this store is ephemeral on App Runner — durable tokens come
    from control_plane.mint_org_token; this is the local/dev twin."""
    import hashlib
    import secrets

    if not (org_id or "").strip():
        return None
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO org_tokens (token_hash, org_id, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, org_id.strip(), (label or "")[:80], time.time()),
        )
    return raw


def rotate_org_token(org_id: str, label: str) -> str | None:
    """Atomically replace one labelled SQLite bearer and return it once."""
    import hashlib
    import secrets

    org = (org_id or "").strip()
    token_label = (label or "").strip()[:80]
    if not org or not token_label:
        return None
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    with _LOCK, _connect() as conn:
        conn.execute(
            "DELETE FROM org_tokens WHERE org_id = ? AND label = ?",
            (org, token_label),
        )
        conn.execute(
            "INSERT INTO org_tokens (token_hash, org_id, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, org, token_label, time.time()),
        )
    return raw


def set_org_token(org_id: str, label: str, raw_token: str) -> bool:
    """Atomically replace one labelled bearer with SHA-256(raw).

    Used by retry-safe Slack completion: the raw value is deterministically
    re-derived from the verified install nonce and is never stored.
    """
    import hashlib

    org = (org_id or "").strip()
    token_label = (label or "").strip()[:80]
    raw = (raw_token or "").strip()
    if not org or not token_label or not raw:
        return False
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    with _LOCK, _connect() as conn:
        conn.execute(
            "DELETE FROM org_tokens WHERE org_id = ? AND label = ?",
            (org, token_label),
        )
        conn.execute(
            "INSERT INTO org_tokens (token_hash, org_id, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, org, token_label, time.time()),
        )
    return True



def begin_brain_install(
    org_id: str, avatar_id: str, nonce: str, channel: str = ""
) -> bool:
    """Persist the only nonce the next OAuth completion may claim."""
    org = (org_id or "").strip()
    avatar = (avatar_id or "").strip()
    install_nonce = (nonce or "").strip()
    if not org or not avatar or not install_nonce:
        return False
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT status, config_json FROM org_connections "
            "WHERE org_id = ? AND avatar_id = ? AND provider = 'cedric-brain'",
            (org, avatar),
        ).fetchone()
        config: dict = {}
        status = "pending"
        if row is not None:
            current_status = str(row["status"] or "")
            # An explicit, user-initiated install supersedes a row stuck in
            # "disconnecting": a disconnect that fenced the org token but never
            # finished its cleanup would otherwise lock re-install forever (the
            # /slack/start route turns this False into a bogus 503 "connection
            # persistence failed"). The token is already revoked at that point,
            # so resetting to a fresh pending install is safe — the stale
            # disconnect marker is dropped below. Only a still-connected row
            # keeps "connected" while its OAuth is re-initiated.
            status = "connected" if current_status == "connected" else "pending"
            try:
                config = json.loads(row["config_json"]) if row["config_json"] else {}
            except ValueError:
                config = {}
            if not isinstance(config, dict):
                config = {}
        config.pop("install_tombstone", None)
        config.pop("revoked_install_nonce", None)
        config.pop("disconnect_phase", None)
        config["pending_install_nonce"] = install_nonce
        config["channel"] = (channel or "").strip()
        conn.execute(
            """
            INSERT INTO org_connections
                (org_id, avatar_id, provider, status, config_json, updated_at)
            VALUES (?, ?, 'cedric-brain', ?, ?, ?)
            ON CONFLICT(org_id, avatar_id, provider) DO UPDATE SET
                status = excluded.status,
                config_json = excluded.config_json,
                updated_at = excluded.updated_at
            """,
            (org, avatar, status, json.dumps(config), time.time()),
        )
    return True


def accept_brain_install(
    org_id: str,
    avatar_id: str,
    nonce: str,
    raw_token: str,
    team_id: str,
    channel: str,
    webhook_secret: str,
    webhook_token: str,
) -> str:
    """CAS one completion and bind retries to its exact accepted envelope."""
    import hashlib

    org = (org_id or "").strip()
    avatar = (avatar_id or "").strip()
    install_nonce = (nonce or "").strip()
    raw = (raw_token or "").strip()
    team = (team_id or "").strip()
    callback_secret = (webhook_secret or "").strip()
    # Optional per-org bearer (see control_plane.complete_brain_install): the
    # shipped Cedric callback sends webhook_secret only; an empty token still
    # completes the install and tenancy holds via the per-org HMAC.
    callback_token = (webhook_token or "").strip()
    callback_channel = (channel or "").strip()
    if not all((org, avatar, install_nonce, raw, team, callback_secret)):
        return "invalid"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    secret_hash = hashlib.sha256(callback_secret.encode()).hexdigest()
    peer_hash = hashlib.sha256(callback_token.encode()).hexdigest()
    label = "cedric-slack-install"
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT status, config_json FROM org_connections "
            "WHERE org_id = ? AND avatar_id = ? AND provider = 'cedric-brain'",
            (org, avatar),
        ).fetchone()
        config: dict = {}
        current_status = ""
        if row is not None:
            current_status = str(row["status"] or "")
            try:
                config = json.loads(row["config_json"]) if row["config_json"] else {}
            except ValueError:
                config = {}
            if not isinstance(config, dict):
                config = {}
        if current_status in ("disconnecting", "disconnected") or bool(
            config.get("install_tombstone")
        ):
            return "stale"

        installed = str(config.get("install_nonce") or "")
        pending = str(config.get("pending_install_nonce") or "")
        if pending:
            if pending != install_nonce:
                return "stale"
        elif installed == install_nonce:
            expected = (
                str(config.get("team_id") or ""),
                str(config.get("channel") or ""),
                str(config.get("webhook_secret_sha256") or ""),
                str(config.get("webhook_token_sha256") or ""),
            )
            presented = (team, callback_channel, secret_hash, peer_hash)
            return "replay" if expected == presented else "conflict"
        elif installed:
            return "stale"

        conn.execute(
            "DELETE FROM org_tokens WHERE org_id = ? AND label = ?",
            (org, label),
        )
        conn.execute(
            "INSERT INTO org_tokens (token_hash, org_id, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, org, label, time.time()),
        )
        config.update(
            {
                "team_id": team,
                "channel": callback_channel,
                "install_nonce": install_nonce,
                "webhook_secret_sha256": secret_hash,
                "webhook_token_sha256": peer_hash,
            }
        )
        config.pop("pending_install_nonce", None)
        config.pop("install_tombstone", None)
        config.pop("revoked_install_nonce", None)
        conn.execute(
            """
            INSERT INTO org_connections
                (org_id, avatar_id, provider, status, config_json, updated_at)
            VALUES (?, ?, 'cedric-brain', 'pending', ?, ?)
            ON CONFLICT(org_id, avatar_id, provider) DO UPDATE SET
                status = excluded.status,
                config_json = excluded.config_json,
                updated_at = excluded.updated_at
            """,
            (org, avatar, json.dumps(config), time.time()),
        )
    return "applied"


def complete_brain_install(
    org_id: str,
    avatar_id: str,
    nonce: str,
    raw_token: str,
    team_id: str,
    channel: str,
    webhook_secret: str,
    webhook_token: str,
) -> str:
    """Run SQLite validation, registry sync and connect under one process lock."""
    import hashlib
    from ..cedric import secret_registry

    org = (org_id or "").strip()
    avatar = (avatar_id or "").strip()
    install_nonce = (nonce or "").strip()
    raw = (raw_token or "").strip()
    team = (team_id or "").strip()
    callback_secret = (webhook_secret or "").strip()
    # Optional per-org bearer (see control_plane.complete_brain_install): the
    # shipped Cedric callback sends webhook_secret only; an empty token still
    # completes the install and tenancy holds via the per-org HMAC.
    callback_token = (webhook_token or "").strip()
    callback_channel = (channel or "").strip()
    if not all((org, avatar, install_nonce, raw, team, callback_secret)):
        return "invalid"

    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    secret_hash = hashlib.sha256(callback_secret.encode()).hexdigest()
    peer_hash = hashlib.sha256(callback_token.encode()).hexdigest()
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT status, config_json FROM org_connections "
            "WHERE org_id = ? AND avatar_id = ? AND provider = 'cedric-brain'",
            (org, avatar),
        ).fetchone()
        config: dict = {}
        current_status = ""
        if row is not None:
            current_status = str(row["status"] or "")
            try:
                config = json.loads(row["config_json"]) if row["config_json"] else {}
            except ValueError:
                config = {}
            if not isinstance(config, dict):
                config = {}
        if current_status in ("disconnecting", "disconnected") or bool(
            config.get("install_tombstone")
        ):
            return "stale"

        installed = str(config.get("install_nonce") or "")
        pending = str(config.get("pending_install_nonce") or "")
        replay = False
        if pending:
            if pending != install_nonce:
                return "stale"
        elif installed == install_nonce:
            expected = (
                str(config.get("team_id") or ""),
                str(config.get("channel") or ""),
                str(config.get("webhook_secret_sha256") or ""),
                str(config.get("webhook_token_sha256") or ""),
            )
            presented = (team, callback_channel, secret_hash, peer_hash)
            if expected != presented:
                return "conflict"
            replay = True
        elif installed:
            return "stale"

        # Keep the process-wide RLock while updating the external registry.
        # A concurrent start/disconnect therefore cannot make this callback
        # stale between validation and the SSM write.
        if not secret_registry.upsert_org_credentials(
            org, callback_secret, callback_token
        ):
            return "registry_failed"

        conn.execute(
            "DELETE FROM org_tokens "
            "WHERE org_id = ? AND label = 'cedric-slack-install'",
            (org,),
        )
        conn.execute(
            "INSERT INTO org_tokens (token_hash, org_id, label, created_at) "
            "VALUES (?, ?, 'cedric-slack-install', ?)",
            (token_hash, org, time.time()),
        )
        config.update(
            {
                "team_id": team,
                "channel": callback_channel,
                "install_nonce": install_nonce,
                "webhook_secret_sha256": secret_hash,
                "webhook_token_sha256": peer_hash,
            }
        )
        config.pop("pending_install_nonce", None)
        config.pop("install_tombstone", None)
        config.pop("revoked_install_nonce", None)
        config.pop("disconnect_phase", None)
        conn.execute(
            """
            INSERT INTO org_connections
                (org_id, avatar_id, provider, status, config_json, updated_at)
            VALUES (?, ?, 'cedric-brain', 'connected', ?, ?)
            ON CONFLICT(org_id, avatar_id, provider) DO UPDATE SET
                status = excluded.status,
                config_json = excluded.config_json,
                updated_at = excluded.updated_at
            """,
            (org, avatar, json.dumps(config), time.time()),
        )
    return "replay" if replay else "applied"


def finish_brain_install(org_id: str, avatar_id: str, nonce: str) -> str:
    """Move the accepted install to connected under the same SQLite lock."""
    org = (org_id or "").strip()
    avatar = (avatar_id or "").strip()
    install_nonce = (nonce or "").strip()
    if not org or not avatar or not install_nonce:
        return "invalid"
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT status, config_json FROM org_connections "
            "WHERE org_id = ? AND avatar_id = ? AND provider = 'cedric-brain'",
            (org, avatar),
        ).fetchone()
        if row is None:
            return "stale"
        try:
            config = json.loads(row["config_json"]) if row["config_json"] else {}
        except ValueError:
            config = {}
        if not isinstance(config, dict):
            config = {}
        if str(row["status"] or "") in ("disconnecting", "disconnected") or bool(
            config.get("install_tombstone")
        ):
            return "stale"
        if str(config.get("pending_install_nonce") or ""):
            return "stale"
        if str(config.get("install_nonce") or "") != install_nonce:
            return "stale"
        conn.execute(
            "UPDATE org_connections SET status = 'connected', updated_at = ? "
            "WHERE org_id = ? AND avatar_id = ? AND provider = 'cedric-brain'",
            (time.time(), org, avatar),
        )
    return "connected"


def begin_brain_disconnect(
    org_id: str, avatar_id: str, phase: str = "revoke_pending"
) -> bool:
    """Fence every SQLite brain row before org-wide credential cleanup."""
    org = (org_id or "").strip()
    requested_avatar = (avatar_id or "").strip()
    if not org or not requested_avatar:
        return False
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT avatar_id, config_json FROM org_connections "
            "WHERE org_id = ? AND provider = 'cedric-brain'",
            (org,),
        ).fetchall()
        if not rows or requested_avatar not in {str(row["avatar_id"]) for row in rows}:
            return False
        # Revoke the org-wide Cedric→Laura bearer in the same SQLite
        # transaction/RLock that fences every connection row.
        conn.execute(
            "DELETE FROM org_tokens "
            "WHERE org_id = ? AND label = 'cedric-slack-install'",
            (org,),
        )
        for row in rows:
            try:
                config = json.loads(row["config_json"]) if row["config_json"] else {}
            except ValueError:
                config = {}
            if not isinstance(config, dict):
                config = {}
            config["disconnect_phase"] = (phase or "revoke_pending").strip()
            conn.execute(
                "UPDATE org_connections SET status = 'disconnecting', "
                "config_json = ?, updated_at = ? "
                "WHERE org_id = ? AND avatar_id = ? AND provider = 'cedric-brain'",
                (json.dumps(config), time.time(), org, str(row["avatar_id"])),
            )
    return True


def tombstone_brain_install(org_id: str, avatar_id: str) -> bool:
    """Revoke the org token and tombstone every SQLite brain row atomically."""
    org = (org_id or "").strip()
    requested_avatar = (avatar_id or "").strip()
    if not org or not requested_avatar:
        return False
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT avatar_id, config_json FROM org_connections "
            "WHERE org_id = ? AND provider = 'cedric-brain'",
            (org,),
        ).fetchall()
        if not rows or requested_avatar not in {str(row["avatar_id"]) for row in rows}:
            return False
        conn.execute(
            "DELETE FROM org_tokens "
            "WHERE org_id = ? AND label = 'cedric-slack-install'",
            (org,),
        )
        for row in rows:
            try:
                old = json.loads(row["config_json"]) if row["config_json"] else {}
            except ValueError:
                old = {}
            if not isinstance(old, dict):
                old = {}
            revoked_nonce = str(
                old.get("pending_install_nonce")
                or old.get("install_nonce")
                or old.get("revoked_install_nonce")
                or ""
            )
            tombstone = {"install_tombstone": True}
            if revoked_nonce:
                tombstone["revoked_install_nonce"] = revoked_nonce
            conn.execute(
                "UPDATE org_connections SET status = 'disconnected', "
                "config_json = ?, updated_at = ? "
                "WHERE org_id = ? AND avatar_id = ? AND provider = 'cedric-brain'",
                (
                    json.dumps(tombstone),
                    time.time(),
                    org,
                    str(row["avatar_id"]),
                ),
            )
    return True


def revoke_org_tokens(org_id: str, label: str = "") -> bool:
    """Revoke SQLite machine bearers for an org."""
    org = (org_id or "").strip()
    if not org:
        return False
    with _LOCK, _connect() as conn:
        if (label or "").strip():
            conn.execute(
                "DELETE FROM org_tokens WHERE org_id = ? AND label = ?",
                (org, label.strip()[:80]),
            )
        else:
            conn.execute("DELETE FROM org_tokens WHERE org_id = ?", (org,))
    return True


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
CONNECTION_STATUSES = ("connected", "pending", "disconnecting", "disconnected")


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


# ── per-org Google OAuth for the native executor ──
# The refresh token is stored ENCRYPTED at rest (never plaintext) — see
# backend/app/crypto.py (Fernet) + NATIVE-INTEGRATIONS-PLAN.md.


def _oauth_enc_secret() -> str:
    """Key for encrypting per-org Google refresh tokens at rest (org_oauth).

    Prefers the dedicated GOOGLE_TOKEN_ENC_KEY — set it to a random value sourced
    from SSM SecureString / KMS in prod so it rotates independently of the session
    cookie key. Falls back to the session secret so tokens are still never stored
    in plaintext without extra config. It never encrypts under a public constant:
    with neither set it fails closed (raises) rather than protect a live OAuth
    token with a shared default. This path is only reachable through a real Google
    OAuth connect — the zero-key demo never stores a token — so the raise cannot
    hit the demo, and its one caller (main.oauth callback) treats it as a
    best-effort skip."""
    key = (settings.google_token_enc_key or "").strip() or (settings.session_secret or "").strip()
    if not key:
        raise RuntimeError(
            "OAuth token encryption key missing: set GOOGLE_TOKEN_ENC_KEY "
            "(preferred) or SESSION_SECRET before connecting Google for the "
            "native executor"
        )
    return key


def set_org_oauth(
    org_id: str, refresh_token: str, *, provider: str = "google",
    email: str = "", scopes: str = "",
) -> bool:
    """Persist (upsert) an org's Google refresh token, encrypted. Empty org or
    token is a no-op (False)."""
    from .. import crypto

    org = (org_id or "").strip()
    rt = (refresh_token or "").strip()
    if not org or not rt:
        return False
    enc = crypto.encrypt(rt, _oauth_enc_secret())
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO org_oauth
                   (org_id, provider, refresh_token_enc, email, scopes, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(org_id, provider) DO UPDATE SET
                 refresh_token_enc=excluded.refresh_token_enc,
                 email=excluded.email, scopes=excluded.scopes,
                 updated_at=excluded.updated_at""",
            (org, provider, enc, (email or "").strip().lower(),
             (scopes or "").strip(), time.time()),
        )
    return True


def get_org_oauth(org_id: str, *, provider: str = "google") -> dict | None:
    """An org's stored OAuth: {refresh_token, email, scopes, updated_at} with the
    token DECRYPTED, or None when there is no usable row (missing or a token that
    can't be decrypted — e.g. the enc key rotated, which reads as 'reconnect')."""
    from .. import crypto

    org = (org_id or "").strip()
    if not org:
        return None
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT refresh_token_enc, email, scopes, updated_at
                   FROM org_oauth WHERE org_id=? AND provider=?""",
            (org, provider),
        ).fetchone()
    if not row or not row["refresh_token_enc"]:
        return None
    try:
        rt = crypto.decrypt(row["refresh_token_enc"], _oauth_enc_secret())
    except Exception:
        return None
    return {
        "refresh_token": rt,
        "email": row["email"] or "",
        "scopes": row["scopes"] or "",
        "updated_at": row["updated_at"],
    }


def clear_org_oauth(org_id: str, *, provider: str = "google") -> bool:
    """Delete an org's stored OAuth for a provider — the NATIVE disconnect.

    Removes the encrypted per-org refresh token so the native executor can no
    longer act on that Google account. Returns True when a row was removed,
    False for an empty org or a no-op (nothing was connected). Pure SQLite,
    keyed by the org_id string — no ``::uuid`` cast, so it is safe for both the
    u_hash session orgs and durable uuid orgs (no split-brain crash)."""
    org = (org_id or "").strip()
    if not org:
        return False
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM org_oauth WHERE org_id=? AND provider=?",
            (org, provider),
        )
    return cur.rowcount > 0


# ── per-USER OAuth (personal-calendar view) ────────────────────────────────
# Same encrypt-at-rest contract as the org_oauth trio above, but keyed by the
# connecting human's user_id so a shared-org colleague never reads it. Used by
# the /dashboard/upcoming VIEW; the native executor keeps using org_oauth.

def set_user_oauth(
    user_id: str, refresh_token: str, *, provider: str = "google",
    email: str = "", scopes: str = "",
) -> bool:
    """Persist (upsert) a USER's Google refresh token, encrypted. Empty user or
    token is a no-op (False)."""
    from .. import crypto

    uid = (user_id or "").strip()
    rt = (refresh_token or "").strip()
    if not uid or not rt:
        return False
    enc = crypto.encrypt(rt, _oauth_enc_secret())
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO user_oauth
                   (user_id, provider, refresh_token_enc, email, scopes, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(user_id, provider) DO UPDATE SET
                 refresh_token_enc=excluded.refresh_token_enc,
                 email=excluded.email, scopes=excluded.scopes,
                 updated_at=excluded.updated_at""",
            (uid, provider, enc, (email or "").strip().lower(),
             (scopes or "").strip(), time.time()),
        )
    return True


def get_user_oauth(user_id: str, *, provider: str = "google") -> dict | None:
    """A user's stored OAuth: {refresh_token, email, scopes, updated_at} with the
    token DECRYPTED, or None when there is no usable row (missing or a token that
    can't be decrypted — reads as 'reconnect', same as get_org_oauth)."""
    from .. import crypto

    uid = (user_id or "").strip()
    if not uid:
        return None
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT refresh_token_enc, email, scopes, updated_at
                   FROM user_oauth WHERE user_id=? AND provider=?""",
            (uid, provider),
        ).fetchone()
    if not row or not row["refresh_token_enc"]:
        return None
    try:
        rt = crypto.decrypt(row["refresh_token_enc"], _oauth_enc_secret())
    except Exception:
        return None
    return {
        "refresh_token": rt,
        "email": row["email"] or "",
        "scopes": row["scopes"] or "",
        "updated_at": row["updated_at"],
    }


def clear_user_oauth(user_id: str, *, provider: str = "google") -> bool:
    """Delete a user's stored personal-calendar OAuth. Returns True when a row was
    removed, False for an empty user or a no-op."""
    uid = (user_id or "").strip()
    if not uid:
        return False
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_oauth WHERE user_id=? AND provider=?",
            (uid, provider),
        )
    return cur.rowcount > 0

def org_for_email(email: str) -> str | None:
    """The org that owns an email address, or None when unknown.

    PERSONAL-FIRST: the org of a REGISTERED USER with that email wins — after
    the personal-orgs cutover that is the person's own durable org, so the
    minutes meter — and any actions — land on the owner, never on a shared
    legacy org. The org-level Google connection (org_oauth.email) is the
    fallback for addresses that never logged in but were connected by an org
    (legacy/shared inboxes). Callers use this to attribute an inbound meeting
    (calendar/email invite) to its owner instead of the Demo org."""
    addr = (email or "").strip().lower()
    if not addr:
        return None
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT org_id FROM users WHERE email=? LIMIT 1",
            (addr,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """SELECT org_id FROM org_oauth WHERE email=?
                       ORDER BY updated_at DESC LIMIT 1""",
                (addr,),
            ).fetchone()
    org = str(row["org_id"]).strip() if row and row["org_id"] else ""
    return org or None


def register_recall_realtime_capability(bot_id: str, capability: str) -> bool:
    """Persist a one-bot realtime capability as SHA-256(raw).

    The raw token only exists in Recall's endpoint URL and the request query;
    it is never stored or logged.
    """
    import hashlib

    bot = (bot_id or "").strip()
    raw = (capability or "").strip()
    if not bot or not raw:
        return False
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with _LOCK, _connect() as conn:
        conn.execute(
            "DELETE FROM recall_realtime_capabilities WHERE bot_id = ?",
            (bot,),
        )
        conn.execute(
            "INSERT INTO recall_realtime_capabilities "
            "(capability_hash, bot_id, created_at) VALUES (?, ?, ?)",
            (digest, bot, time.time()),
        )
    return True


# "gemini" retired from the selectable set (owner ask 2026-07-22, after the
# 2026-07-21 hijack: a store row lost across deploys let an avatar fall onto
# the Gemini relay, which bypasses persona/registry/playbooks). Gemini stays
# reachable ONLY via the global GEMINI_EARS_MODE env — a deliberate operator
# choice, never a dashboard click or a stale row.
_VALID_BRAIN_MODES = {"cerebras"}


def set_avatar_brain_mode(avatar_id: str, brain_mode: str) -> bool:
    """Set an avatar's brain: "cerebras" (the normal Deepgram + grounded brain)
    is the only selectable value. Persisted (Litestream-replicated), read on
    the NEXT meeting — no redeploy."""
    aid = (avatar_id or "").strip()
    mode = (brain_mode or "").strip().lower()
    if not aid or mode not in _VALID_BRAIN_MODES:
        return False
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO avatar_brain_mode (avatar_id, brain_mode, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(avatar_id) DO UPDATE SET "
            "brain_mode = excluded.brain_mode, updated_at = excluded.updated_at",
            (aid, mode, time.time()),
        )
    return True


def get_avatar_brain_mode(avatar_id: str) -> str | None:
    """The avatar's explicit brain choice, or None if it has never been set
    (caller falls back to the global default). Values outside the selectable
    set (legacy "gemini" rows) read as None — a stale row must never
    resurrect the relay brain (the 2026-07-21 hijack)."""
    aid = (avatar_id or "").strip()
    if not aid:
        return None
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT brain_mode FROM avatar_brain_mode WHERE avatar_id = ?", (aid,)
        ).fetchone()
    mode = row[0] if row else None
    return mode if mode in _VALID_BRAIN_MODES else None


def set_org_pref(org_id: str, key: str, value: str) -> bool:
    """Upsert one per-org preference switch (short string values only)."""
    org = (org_id or "").strip()
    k = (key or "").strip()
    if not org or not k:
        return False
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO org_prefs (org_id, key, value, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(org_id, key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (org, k, str(value or ""), time.time()),
        )
    return True


def get_org_pref(org_id: str, key: str) -> str | None:
    """The org's stored preference value, or None when never set (caller
    applies the default — e.g. show_transcripts defaults OFF)."""
    org = (org_id or "").strip()
    k = (key or "").strip()
    if not org or not k:
        return None
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT value FROM org_prefs WHERE org_id = ? AND key = ?", (org, k)
        ).fetchone()
    return row[0] if row else None


# ── first-class decisions (0017 / meeting_decisions) ──────────────────────
# A decision gets its own durable identity so the archive can answer "who
# decided this, why, and does it supersede an earlier one?". Durable orgs route
# to Postgres (RLS-scoped); the key-free/demo world persists in SQLite. Never
# store transcript text here — source_ref is a bot_id/meeting_key ONLY.
_VALID_DECISION_STATUS = ("active", "superseded", "revisited")

# A new decision OVERRIDES an earlier one when its text carries an explicit
# supersede cue. Deterministic regex (no model call) — the linker only fires
# when the cue AND a shared related_project are both present.
_DECISION_SUPERSEDE_CUE = re.compile(
    r"\b(supersed\w+|instead of|changed from|no longer|moved to|replaces?\b|"
    r"overrid\w+|rather than|in place of)\b",
    re.IGNORECASE,
)

_DECISION_FIELDS = (
    "id", "bot_id", "decision", "decision_maker", "reason",
    "related_project", "supersedes", "status", "source_ref",
    "created_at", "updated_at",
)


def _decision_row_to_dict(row: "sqlite3.Row") -> dict:
    d = {k: row[k] for k in _DECISION_FIELDS}
    # Present empty strings as None for the optional link, so consumers can test
    # `if d["supersedes"]` uniformly with the Postgres (nullable uuid) shape.
    for k in ("supersedes", "decision_maker", "reason", "related_project"):
        if d.get(k) == "":
            d[k] = None
    return d


def _decision_durable(org_id: str) -> bool:
    """Route this org's decisions to Postgres? (mirror of the artifact rule)."""
    from . import control_plane

    return durable_artifacts_enabled() and control_plane.is_durable_org(
        str(org_id)
    )


def save_decision(
    org_id: str,
    bot_id: str,
    record: dict,
    *,
    decision_id: str | None = None,
) -> str | None:
    """Persist one first-class decision record; returns its id.

    ``record`` carries decision (required), decision_maker, reason,
    related_project, supersedes, status, source_ref. ``source_ref`` must be a
    bot_id / meeting_key — NEVER transcript text (PII). Durable orgs write to
    Postgres under RLS; everything else writes SQLite."""
    org = (org_id or "").strip() or DEMO_ORG_ID
    decision_text = str(record.get("decision") or "").strip()
    if not decision_text:
        return None
    did = decision_id or str(uuid.uuid4())
    status = str(record.get("status") or "active")
    if status not in _VALID_DECISION_STATUS:
        status = "active"
    now = time.time()
    payload = {
        "id": did,
        "bot_id": str(bot_id or ""),
        "decision": decision_text,
        "decision_maker": str(record.get("decision_maker") or ""),
        "reason": str(record.get("reason") or ""),
        "related_project": str(record.get("related_project") or ""),
        "supersedes": str(record.get("supersedes") or ""),
        "status": status,
        "source_ref": str(record.get("source_ref") or bot_id or ""),
    }

    if _decision_durable(org):
        from . import control_plane

        control_plane.save_decision(org, payload)
        return did

    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO meeting_decisions
                (org_id, id, bot_id, decision, decision_maker, reason,
                 related_project, supersedes, status, source_ref,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(org_id, id) DO UPDATE SET
                bot_id=excluded.bot_id,
                decision=excluded.decision,
                decision_maker=excluded.decision_maker,
                reason=excluded.reason,
                related_project=excluded.related_project,
                supersedes=excluded.supersedes,
                status=excluded.status,
                source_ref=excluded.source_ref,
                updated_at=excluded.updated_at
            """,
            (
                org, did, payload["bot_id"], payload["decision"],
                payload["decision_maker"], payload["reason"],
                payload["related_project"], payload["supersedes"],
                payload["status"], payload["source_ref"], now, now,
            ),
        )
    return did


def list_decisions(org_id: str, bot_id: str | None = None) -> list[dict]:
    """Decision records for an org, newest first. When ``bot_id`` is given,
    only that meeting's decisions; otherwise the org's whole decision history
    (used by the supersede linker to find earlier decisions to override)."""
    org = (org_id or "").strip() or DEMO_ORG_ID
    if _decision_durable(org):
        from . import control_plane

        return control_plane.list_decisions(org, bot_id) or []

    with _LOCK, _connect() as conn:
        if bot_id is None:
            rows = conn.execute(
                "SELECT * FROM meeting_decisions WHERE org_id = ? "
                "ORDER BY created_at DESC, id DESC",
                (org,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM meeting_decisions WHERE org_id = ? AND bot_id = ? "
                "ORDER BY created_at DESC, id DESC",
                (org, str(bot_id)),
            ).fetchall()
    return [_decision_row_to_dict(r) for r in rows]


def get_decision(org_id: str, decision_id: str) -> dict | None:
    """One decision by id, tenant-scoped."""
    org = (org_id or "").strip() or DEMO_ORG_ID
    did = (decision_id or "").strip()
    if not did:
        return None
    if _decision_durable(org):
        from . import control_plane

        return control_plane.get_decision(org, did)
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM meeting_decisions WHERE org_id = ? AND id = ?",
            (org, did),
        ).fetchone()
    return _decision_row_to_dict(row) if row is not None else None


def mark_superseded(org_id: str, decision_id: str, *, status: str = "superseded") -> bool:
    """Flip an earlier decision's status (default 'superseded') when a newer
    decision overrides it. The row is kept — decisions are history, never
    deleted."""
    org = (org_id or "").strip() or DEMO_ORG_ID
    did = (decision_id or "").strip()
    if not did or status not in _VALID_DECISION_STATUS:
        return False
    if _decision_durable(org):
        from . import control_plane

        return bool(control_plane.mark_superseded(org, did, status))
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "UPDATE meeting_decisions SET status = ?, updated_at = ? "
            "WHERE org_id = ? AND id = ?",
            (status, time.time(), org, did),
        )
    return cur.rowcount > 0


def persist_decision_records(
    org_id: str, bot_id: str, records: list[dict]
) -> list[str]:
    """Save a finished meeting's decision_records and wire supersede links.

    For each new record we save a row, then — deterministically, no model call
    — check for an explicit supersede cue in the decision text. When the cue
    fires AND the record names a related_project, the most recent EARLIER active
    decision on that same project (from prior meetings or earlier in this batch)
    is linked: the new row's ``supersedes`` points at it and the old row flips to
    ``status='superseded'``. Both never stay 'active'. Returns the new ids.

    Off the live path (finalize/threadpool). Best-effort at the call site —
    persistence must never break the meter-stop. Never logs decision text."""
    org = (org_id or "").strip() or DEMO_ORG_ID
    saved_ids: list[str] = []
    if not records:
        return saved_ids
    # Prior active decisions in this org, by project, most-recent-first. Built
    # once; updated in-memory as this batch supersedes earlier ones.
    prior = [d for d in list_decisions(org) if (d.get("status") == "active")]
    for rec in records:
        if not isinstance(rec, dict):
            continue
        text = str(rec.get("decision") or "").strip()
        if not text:
            continue
        project = str(rec.get("related_project") or "").strip()
        target = None
        if project and _DECISION_SUPERSEDE_CUE.search(text):
            key = project.casefold()
            for cand in prior:
                if (
                    str(cand.get("related_project") or "").strip().casefold() == key
                    and cand.get("status") == "active"
                ):
                    target = cand
                    break
        rec_to_save = dict(rec)
        rec_to_save["source_ref"] = str(rec.get("source_ref") or bot_id or "")
        if target is not None:
            rec_to_save["supersedes"] = str(target.get("id") or "")
        new_id = save_decision(org, bot_id, rec_to_save)
        if new_id is None:
            continue
        saved_ids.append(new_id)
        if target is not None:
            mark_superseded(org, str(target.get("id")))
            target["status"] = "superseded"  # keep the in-memory view honest
        # This new decision becomes a supersede candidate for later records.
        prior.insert(0, {
            "id": new_id,
            "related_project": project,
            "status": "active",
        })
    return saved_ids


def all_avatar_brain_modes() -> dict[str, str]:
    """{avatar_id: brain_mode} for every avatar with an explicit choice."""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT avatar_id, brain_mode FROM avatar_brain_mode"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


# The capabilities an avatar can independently toggle. An integration is
# CONNECTED once at the org level (Connections view); each avatar then flips
# whether it may USE it. Mirrors the brain-mode primitives above.
KNOWN_CAPABILITIES = ("google", "slack", "asana")

# Beyond the native trio, any Pipedream-connected app is toggleable per avatar
# under its Pipedream name_slug (github, notion, linear, …). Slug-shaped keys
# only — this is a storage guard, not an existence check (the dashboard only
# offers slugs the org actually connected).
import re as _re

_CAPABILITY_SLUG = _re.compile(r"^[a-z0-9_][a-z0-9_-]{0,59}$")


def set_avatar_capability(avatar_id: str, capability: str, enabled: bool) -> bool:
    """Turn one capability ON/OFF for one avatar (dashboard toggle). Persisted
    (Litestream-replicated), read at the execute/deliver seams — no redeploy.
    False for a key that is neither a KNOWN capability nor slug-shaped."""
    aid = (avatar_id or "").strip()
    cap = (capability or "").strip().lower()
    if not aid or (cap not in KNOWN_CAPABILITIES and not _CAPABILITY_SLUG.match(cap)):
        return False
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO avatar_capabilities "
            "(avatar_id, capability, enabled, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(avatar_id, capability) DO UPDATE SET "
            "enabled = excluded.enabled, updated_at = excluded.updated_at",
            (aid, cap, 1 if enabled else 0, time.time()),
        )
    return True


def get_avatar_capabilities(avatar_id: str) -> dict[str, bool]:
    """The avatar's EXPLICIT capability switches as ``{capability: bool}``.

    Only capabilities the owner has actually toggled appear. A capability
    ABSENT from the map has never been set — the caller applies the default:
    ON when the org has that integration connected, else off (see
    ``capability_enabled``). Enforcement reads this raw and skips only on an
    explicit ``False`` (so an untouched avatar keeps today's behaviour)."""
    aid = (avatar_id or "").strip()
    if not aid:
        return {}
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT capability, enabled FROM avatar_capabilities WHERE avatar_id = ?",
            (aid,),
        ).fetchall()
    return {r[0]: bool(r[1]) for r in rows}


def capability_enabled(avatar_id: str, capability: str, *, connected: bool) -> bool:
    """Resolve whether an avatar MAY use a capability: the explicit per-avatar
    switch, defaulting to the org-level ``connected`` state when never toggled.
    This is the "default ON when connected, else off" rule in one place."""
    return get_avatar_capabilities(avatar_id).get(capability, connected)


# ── canonical approval records (handshake operation: approve-action) ──

def record_action_approval(
    org_id: str, action_id: str, *, decision: str, selected_slot_id: str = "",
    idempotency_key: str = "", decided_via: str = "", laura_user_id: str = "",
    previous_status: str = "", new_status: str = "",
    execution_job_id: str | None = None, blocked_on: str = "",
) -> bool:
    """Persist the ONE canonical decision for an action. First write wins —
    the approve door answers replays/conflicts from the stored row, so this
    deliberately refuses to overwrite (INSERT OR IGNORE + rowcount)."""
    org = (org_id or "").strip()
    aid = (action_id or "").strip()
    if not org or not aid or not decision:
        return False
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO action_approvals "
            "(org_id, action_id, decision, selected_slot_id, idempotency_key, "
            " decided_via, laura_user_id, previous_status, new_status, "
            " execution_job_id, blocked_on, decided_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (org, aid, decision, selected_slot_id, idempotency_key, decided_via,
             laura_user_id, previous_status, new_status, execution_job_id,
             blocked_on, time.time()),
        )
    return cur.rowcount > 0


def get_action_approval(org_id: str, action_id: str) -> dict | None:
    """The recorded canonical decision for (org, action), or None."""
    org = (org_id or "").strip()
    aid = (action_id or "").strip()
    if not org or not aid:
        return None
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM action_approvals WHERE org_id=? AND action_id=?",
            (org, aid),
        ).fetchone()
    return dict(row) if row else None


def set_action_approval_job(org_id: str, action_id: str, execution_job_id: str) -> None:
    """Stamp the execution job once a blocked/deferred approval finally runs."""
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE action_approvals SET execution_job_id=?, blocked_on='' "
            "WHERE org_id=? AND action_id=?",
            (execution_job_id, (org_id or "").strip(), (action_id or "").strip()),
        )


# ── dashboard chat channel (org ↔ Cedric, the in-dashboard approval surface) ──

_CHAT_SENDERS = {"user", "cedric", "system"}
_CHAT_BODY_MAX = 8000  # distilled content only; a runaway body never bloats the page


def add_chat_message(
    org_id: str,
    sender: str,
    *,
    body: str = "",
    sender_label: str = "",
    kind: str = "text",
    action_id: str = "",
    payload: dict | None = None,
) -> dict | None:
    """Append one message to the org's channel. Returns the stored row (with
    id) or None on bad input. Decisions NEVER live here — an action card only
    references its action_id; the canonical decision is action_approvals."""
    org = (org_id or "").strip()
    text = (body or "").strip()[:_CHAT_BODY_MAX]
    if not org or sender not in _CHAT_SENDERS:
        return None
    if not text and kind == "text":
        return None
    now = time.time()
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (org_id, sender, sender_label, kind, "
            "body, action_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                org, sender, (sender_label or "")[:120], (kind or "text")[:32],
                text, (action_id or "")[:64],
                json.dumps(payload, separators=(",", ":")) if payload else "",
                now,
            ),
        )
        mid = int(cur.lastrowid)
    return {
        "id": mid, "sender": sender, "sender_label": (sender_label or "")[:120],
        "kind": (kind or "text")[:32], "body": text,
        "action_id": (action_id or "")[:64], "payload": payload or {},
        "created_at": now,
    }


def list_chat_messages(org_id: str, after_id: int = 0, limit: int = 200) -> list[dict]:
    """Messages after `after_id`, oldest-first (the chat poll cursor)."""
    org = (org_id or "").strip()
    if not org:
        return []
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE org_id = ? AND id > ? "
            "ORDER BY id LIMIT ?",
            (org, int(after_id or 0), max(1, min(int(limit or 200), 500))),
        ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except ValueError:
            payload = {}
        out.append(
            {
                "id": r["id"], "sender": r["sender"],
                "sender_label": r["sender_label"], "kind": r["kind"],
                "body": r["body"], "action_id": r["action_id"],
                "payload": payload, "created_at": r["created_at"],
            }
        )
    return out


def list_blocked_action_approvals(org_id: str) -> list[dict]:
    """Every approve-decision in this org still parked behind unmet
    dependencies ([M8]) — the work-list for the deferred release.

    Scoped to one org and to rows that ARE blocked, so the release sweep needs
    no cross-tenant discovery: `blocked_on` is cleared the moment the approval
    really runs, which keeps this list naturally tiny."""
    org = (org_id or "").strip()
    if not org:
        return []
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT action_id, blocked_on FROM action_approvals "
            "WHERE org_id=? AND decision='approve' AND blocked_on NOT IN ('', '[]')",
            (org,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_action_approval_blocked_on(
    org_id: str, action_id: str, blocked_on: str
) -> None:
    """Re-park a dependency-blocked approval on a SHRUNKEN dependency list (or
    clear it with '[]'). The decision itself is never touched — only what the
    approval is still waiting for."""
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE action_approvals SET blocked_on=? WHERE org_id=? AND action_id=?",
            (blocked_on or "", (org_id or "").strip(), (action_id or "").strip()),
        )


def set_action_approval_result(
    org_id: str, action_id: str, *, new_status: str = "",
    execution_job_id: str | None = None,
) -> None:
    """Settle the execution outcome onto the recorded decision row (two-phase
    canonical approve: the decision is recorded BEFORE dispatch, the outcome
    fields land here after). The decision itself is never overwritten, and a
    dependency-blocked approve keeps its blocked_on list until it really runs."""
    sets, args = [], []
    if new_status:
        sets.append("new_status=?")
        args.append(new_status)
    if execution_job_id:
        sets.append("execution_job_id=?")
        args.append(execution_job_id)
        sets.append("blocked_on=''")
    if not sets:
        return
    with _LOCK, _connect() as conn:
        conn.execute(
            f"UPDATE action_approvals SET {', '.join(sets)} "
            "WHERE org_id=? AND action_id=?",
            (*args, (org_id or "").strip(), (action_id or "").strip()),
        )


# ── typed-spec overrides (canonical Action param edits, key-free mode) ──
# Durable orgs persist edited params on queued_actions.typed_json (Postgres);
# the key-free/demo path keeps the SAME feature via this small overlay table
# so the artifact itself stays an immutable historical record.

def set_action_typed_override(org_id: str, action_id: str, typed: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO action_typed_overrides
                   (org_id, action_id, typed_json, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(org_id, action_id) DO UPDATE SET
                 typed_json=excluded.typed_json, updated_at=excluded.updated_at""",
            (
                (org_id or "").strip(), (action_id or "").strip(),
                json.dumps(typed, separators=(",", ":"), sort_keys=True),
                time.time(),
            ),
        )


def get_action_typed_override(org_id: str, action_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT typed_json FROM action_typed_overrides "
            "WHERE org_id=? AND action_id=?",
            ((org_id or "").strip(), (action_id or "").strip()),
        ).fetchone()
    if row is None:
        return None
    try:
        typed = json.loads(row["typed_json"])
    except ValueError:
        return None
    return typed if isinstance(typed, dict) else None


def is_org_member(user_id: str, org_id: str) -> bool:
    """Active membership check for the approve door's approver rule. A
    personal org (org_id == user_id) needs no membership row."""
    uid = (user_id or "").strip()
    org = (org_id or "").strip()
    if not uid or not org:
        return False
    if uid == org:
        return True
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM memberships WHERE user_id=? AND org_id=? "
            "AND status='active'",
            (uid, org),
        ).fetchone()
    return row is not None


def all_avatar_capabilities() -> dict[str, dict[str, bool]]:
    """{avatar_id: {capability: bool}} for every avatar with any explicit
    switch — the bulk read the dashboard summary uses (one query, no N+1)."""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT avatar_id, capability, enabled FROM avatar_capabilities"
        ).fetchall()
    out: dict[str, dict[str, bool]] = {}
    for r in rows:
        out.setdefault(r[0], {})[r[1]] = bool(r[2])
    return out


def resolve_recall_realtime_capability(capability: str) -> str | None:
    """Return the single bot bound to raw capability, else None."""
    import hashlib

    raw = (capability or "").strip()
    if not raw:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT bot_id FROM recall_realtime_capabilities "
            "WHERE capability_hash = ?",
            (digest,),
        ).fetchone()
    return row["bot_id"] if row else None


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
        conn.execute(
            "DELETE FROM recall_realtime_capabilities WHERE bot_id = ?", (bot_id,)
        )
        conn.execute("DELETE FROM sessions WHERE bot_id = ?", (bot_id,))
        conn.execute("DELETE FROM conversation_routes WHERE bot_id = ?", (bot_id,))


_init_db()
_load_from_db()
