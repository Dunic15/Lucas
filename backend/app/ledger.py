"""Cross-meeting memory: the action & decision ledger.

Each finished meeting deposits its extracted facts — action items, decisions,
missing process steps — into a persistent ledger keyed by the *meeting*, so
the next session on the same meeting link starts knowing what is still open:
"the DPA was flagged missing on Jul 1 and never resolved". This is the layer
that turns Laura from a per-meeting notetaker into something that remembers.

Design constraints:
  - PII discipline: the ledger stores the already-distilled artifact fields
    (short action/decision lines), never raw transcript text, and nothing
    here is ever logged.
  - Latency: reads happen once per session (at start / first line), writes
    once (at finalize). Nothing runs per-utterance.
  - Determinism: only template process steps auto-resolve (a step missing in
    meeting N that is no longer missing in meeting N+1 of the same type).
    Free-text actions never auto-resolve — they stay open until marked done
    via the API, so Laura never silently drops a commitment.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from typing import Any

from . import store
from .config import settings
from .meeting_state import humanize_step

# Every ledger row is tenant-owned (docs/infra/MULTI-TENANCY.md §3). org_id is a
# keyword arg on every public function, defaulting to the Demo org so the
# single-tenant/service path stays byte-identical while isolation is enforced
# via `WHERE org_id=?` on every statement. meeting_key() stays a pure URL→code
# function; isolation is (org_id, meeting_key), never meeting_key alone.
DEMO_ORG_ID = settings.demo_org_id


def new_action_id() -> str:
    """A fresh stable id for one action item. Assigned once (at live capture, or
    at finalize for a summarizer-only action) and carried through the
    action.requested webhook, the artifact's actions[], and this ledger row — so
    the orchestrator correlates + dedupes + resolves on it."""
    return uuid.uuid4().hex[:16]

_MEET_CODE = re.compile(r"meet\.google\.com/([a-z\-]+)", re.IGNORECASE)
_ZOOM_CODE = re.compile(r"zoom\.us/j/(\d+)", re.IGNORECASE)
_TEAMS_CODE = re.compile(r"teams\.(?:microsoft|live)\.com/[^\s\"]*meetup-join/([^/?\s]+)", re.IGNORECASE)

_OPEN_KINDS = ("action", "missing_step")
_LIST_LIMIT = 200  # hard cap per meeting_key, oldest-first eviction on insert


def meeting_key(meeting_url: str) -> str:
    """Stable identity for 'the same meeting' across sessions.

    Recurring meetings reuse their link (Meet code / Zoom id / Teams thread),
    so the platform code is the natural key. Unknown formats fall back to the
    normalized URL — still stable for a re-used link.
    """
    url = (meeting_url or "").strip()
    for pattern in (_MEET_CODE, _ZOOM_CODE, _TEAMS_CODE):
        m = pattern.search(url)
        if m:
            return m.group(1).lower()
    return url.rstrip("/").lower()


def _ledger_add_column(conn: sqlite3.Connection, coldef: str) -> None:
    """Idempotent ADD COLUMN (mirrors store._add_column's guarded style)."""
    try:
        conn.execute(f"ALTER TABLE ledger_items ADD COLUMN {coldef}")
    except sqlite3.OperationalError:
        pass  # column already exists


def _init_db() -> None:
    demo = DEMO_ORG_ID
    with store._LOCK, store._connect() as conn:
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS ledger_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id TEXT NOT NULL DEFAULT '{demo}',   -- tenant owner (§3)
                meeting_key TEXT NOT NULL,               -- scoped by org_id now
                avatar_id TEXT NOT NULL,
                kind TEXT NOT NULL,          -- action | decision | missing_step
                item TEXT NOT NULL,          -- distilled line, never raw transcript
                item_norm TEXT NOT NULL DEFAULT '',      -- _norm(item), stored for dedupe
                owner TEXT NOT NULL DEFAULT '',
                deadline TEXT NOT NULL DEFAULT '',
                meeting_type TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',   -- open | done | rejected | failed | noted
                bot_id TEXT NOT NULL DEFAULT '',
                action_id TEXT NOT NULL DEFAULT '',    -- stable cross-channel id (actions)
                created_at REAL NOT NULL,
                resolved_at REAL,
                resolved_by_bot_id TEXT NOT NULL DEFAULT '',
                resolution_detail TEXT NOT NULL DEFAULT ''  -- distilled outcome one-liner
            );
            """
        )
        # Migration for stores created before the action_id column. This MUST
        # run BEFORE the action_id index below: on an EXISTING ledger DB the
        # CREATE TABLE above is a no-op, so the column doesn't exist yet — and
        # `CREATE INDEX ON ledger_items(action_id)` would raise "no such column"
        # and crash the boot. (Fresh-DB tests never hit this ordering because
        # their CREATE TABLE already includes the column.)
        _ledger_add_column(conn, "action_id TEXT NOT NULL DEFAULT ''")
        # Additive migration for stores that predate the resolution_detail
        # column: the short "why" recorded when a resolve carries an outcome
        # ("rejected: budget cut"). Idempotent.
        _ledger_add_column(conn, "resolution_detail TEXT NOT NULL DEFAULT ''")
        # Multi-tenancy: org_id (backfills existing rows to Demo) + a stored
        # item_norm so the dedupe UNIQUE is (org_id, meeting_key, kind, item_norm).
        _ledger_add_column(conn, f"org_id TEXT NOT NULL DEFAULT '{demo}'")
        _ledger_add_column(conn, "item_norm TEXT NOT NULL DEFAULT ''")
        # Backfill item_norm for pre-existing rows (SQLite can't compute _norm
        # in SQL); a fresh DB has no rows so this is a no-op. Needed before the
        # UNIQUE index, else two rows of the same meeting/kind collide on ''.
        for row in conn.execute(
            "SELECT id, item FROM ledger_items WHERE item_norm=''"
        ).fetchall():
            conn.execute(
                "UPDATE ledger_items SET item_norm=? WHERE id=?",
                (_norm(row["item"]), row["id"]),
            )
        # Column now guaranteed to exist (fresh OR migrated) — safe to index.
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_ledger_org_key_status
                ON ledger_items(org_id, meeting_key, status);
            CREATE INDEX IF NOT EXISTS idx_ledger_action_id
                ON ledger_items(action_id);
            """
        )
        # Org-scoped dedupe backstop (the Python `existing` set is the primary
        # guard; this is belt-and-suspenders and matches the Postgres UNIQUE).
        # Guarded: a pre-existing DB with legacy duplicates must not crash boot.
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_dedupe "
                "ON ledger_items(org_id, meeting_key, kind, item_norm)"
            )
        except sqlite3.OperationalError:
            pass  # legacy duplicates — the Python dedupe guard still applies
        # Execution provenance reported back by the orchestrator (Cedric) via
        # POST /org/actions/{action_id}/status: the brain's side of the story
        # (proposed → approved/rejected → done/failed), keyed on the same
        # stable action_id the events carry. Latest state only — the dashboard
        # shows where each action stands, not a full audit trail.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS action_status (
                action_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,        -- proposed | approved | rejected | done | failed
                detail TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            """
        )


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def record_meeting(
    meeting_url: str, avatar_id: str, bot_id: str, artifact: dict[str, Any],
    *, org_id: str = DEMO_ORG_ID,
) -> dict[str, int]:
    """Fold one finished meeting's artifact into the ledger.

    - resolves previously-missing process steps that this meeting covered
      (same org + meeting_key + meeting_type, deterministic via the template),
    - inserts new open actions / missing steps / decisions,
    - dedupes on normalized item text per (org_id, meeting_key, kind).
    Every read/write is scoped to ``org_id`` so two tenants on the same
    recurring link never merge memory. Returns counts for the caller.
    """
    key = meeting_key(meeting_url)
    meeting_type = str(artifact.get("meeting_type") or "")
    now = time.time()
    added = resolved = 0

    current_missing = {str(s) for s in artifact.get("missing_steps") or []}

    with store._LOCK, store._connect() as conn:
        # 1. Resolve: step was open from an earlier session of the same
        #    org+meeting+type, and this meeting no longer lists it as missing.
        if meeting_type:
            open_steps = conn.execute(
                """SELECT id, item FROM ledger_items
                   WHERE org_id=? AND meeting_key=? AND kind='missing_step'
                     AND status='open' AND meeting_type=?""",
                (org_id, key, meeting_type),
            ).fetchall()
            for row in open_steps:
                if row["item"] not in current_missing:
                    conn.execute(
                        """UPDATE ledger_items
                           SET status='done', resolved_at=?, resolved_by_bot_id=?
                           WHERE id=? AND org_id=?""",
                        (now, bot_id, row["id"], org_id),
                    )
                    resolved += 1

        existing = {
            (row["kind"], _norm(row["item"]))
            for row in conn.execute(
                "SELECT kind, item FROM ledger_items WHERE org_id=? AND meeting_key=?",
                (org_id, key),
            ).fetchall()
        }

        def _insert(kind: str, item: str, owner: str = "", deadline: str = "",
                    status: str = "open", action_id: str = "") -> None:
            nonlocal added
            item = (item or "").strip()[:200]
            norm = _norm(item)
            if not item or (kind, norm) in existing:
                return
            resolved_at = None
            resolution_detail = ""
            if kind == "action" and action_id:
                execution = conn.execute(
                    """SELECT status, detail, updated_at FROM action_status
                       WHERE action_id=?""",
                    (action_id,),
                ).fetchone()
                if execution and execution["status"] in _TERMINAL_STATUS_OUTCOME:
                    # Cedric may report completion before finalize creates this
                    # row. Preserve that terminal state instead of resurrecting
                    # the action as open.
                    status = _TERMINAL_STATUS_OUTCOME[execution["status"]]
                    resolved_at = execution["updated_at"]
                    resolution_detail = execution["detail"]
            conn.execute(
                """INSERT INTO ledger_items
                   (org_id, meeting_key, avatar_id, kind, item, item_norm, owner,
                    deadline, meeting_type, status, bot_id, action_id, created_at,
                    resolved_at, resolution_detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (org_id, key, avatar_id, kind, item, norm, owner, deadline,
                 meeting_type, status, bot_id, action_id, now, resolved_at,
                 resolution_detail),
            )
            existing.add((kind, norm))
            added += 1

        for a in artifact.get("actions") or []:
            if isinstance(a, dict):
                _insert("action", a.get("item", ""), a.get("owner", "") or "",
                        a.get("deadline", "") or "", action_id=a.get("action_id", "") or "")
            else:
                _insert("action", str(a))
        for step in current_missing:
            _insert("missing_step", step)
        for d in artifact.get("decisions") or []:
            _insert("decision", str(d), status="noted")

        # keep the ledger bounded per (org, meeting). Org-scoped so eviction
        # never deletes another tenant's rows on a shared meeting_key (§6.5).
        conn.execute(
            """DELETE FROM ledger_items
               WHERE org_id=? AND meeting_key=? AND id NOT IN (
                 SELECT id FROM ledger_items WHERE org_id=? AND meeting_key=?
                 ORDER BY id DESC LIMIT ?)""",
            (org_id, key, org_id, key, _LIST_LIMIT),
        )

    return {"added": added, "resolved": resolved}


def items(
    meeting_key_: str, status: str = "", *, org_id: str = DEMO_ORG_ID
) -> list[dict[str, Any]]:
    q = "SELECT * FROM ledger_items WHERE org_id=? AND meeting_key=?"
    args: list[Any] = [org_id, meeting_key_]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY id"
    with store._LOCK, store._connect() as conn:
        return [dict(row) for row in conn.execute(q, args).fetchall()]


def open_by_meeting(*, org_id: str = DEMO_ORG_ID) -> dict[str, list[dict[str, Any]]]:
    """Every open item for one org across all meetings, grouped by meeting_key
    (for the autopilot nudge digest)."""
    with store._LOCK, store._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM ledger_items WHERE org_id=? AND status='open' "
            "ORDER BY meeting_key, id",
            (org_id,),
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["meeting_key"], []).append(dict(row))
    return grouped


def search(
    query: str, *, limit: int = 20, org_id: str = DEMO_ORG_ID
) -> list[dict[str, Any]]:
    """Ledger items matching a free-text query in the item or owner text, for
    one org. Powers 'what did we decide/commit about X across all meetings'."""
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    with store._LOCK, store._connect() as conn:
        rows = conn.execute(
            """SELECT * FROM ledger_items
               WHERE org_id=? AND (item LIKE ? OR owner LIKE ?)
               ORDER BY status='open' DESC, id DESC LIMIT ?""",
            (org_id, like, like, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# Terminal outcomes a resolve may carry (agreed contract with the orchestrator
# side): all three CLOSE the item — "rejected" and "failed" are as final as
# "done", so a declined or errored action stops resurfacing as open forever.
RESOLUTION_OUTCOMES = ("done", "rejected", "failed")


def resolve_item(
    item_id: int, bot_id: str = "", outcome: str = "done", detail: str = "",
    *, org_id: str = DEMO_ORG_ID,
) -> bool:
    """Close a ledger item with a terminal outcome (default 'done', keeping
    every existing caller's behavior byte-identical). Scoped to ``org_id`` so
    one tenant can never resolve another's row. ``detail`` is a distilled
    one-liner (capped, never transcript content by contract). An unknown
    outcome is a no-op (False) — callers validate first for their 400s."""
    if outcome not in RESOLUTION_OUTCOMES:
        return False
    with store._LOCK, store._connect() as conn:
        cur = conn.execute(
            """UPDATE ledger_items
               SET status=?, resolved_at=?, resolved_by_bot_id=?, resolution_detail=?
               WHERE id=? AND org_id=? AND status='open'""",
            (outcome, time.time(), bot_id, (detail or "").strip()[:300], item_id, org_id),
        )
        return cur.rowcount > 0


def resolve_by_action_id(
    action_id: str, bot_id: str = "", outcome: str = "done", detail: str = "",
    *, org_id: str = DEMO_ORG_ID,
) -> bool:
    """Close an action by its stable cross-channel action_id — the id the
    orchestrator (Cedric) holds from the live action.requested event and the
    session.ended artifact, so it can ack 'done/approved in Slack' without ever
    seeing the numeric ledger row id. Scoped to ``org_id``. Only resolves after
    the meeting finalized (when the row exists); an unknown/already-closed id or
    outcome is a no-op (False). ``outcome``/``detail`` semantics match
    resolve_item."""
    aid = (action_id or "").strip()
    if not aid or outcome not in RESOLUTION_OUTCOMES:
        return False
    with store._LOCK, store._connect() as conn:
        cur = conn.execute(
            """UPDATE ledger_items
               SET status=?, resolved_at=?, resolved_by_bot_id=?, resolution_detail=?
               WHERE action_id=? AND org_id=? AND status='open'""",
            (outcome, time.time(), bot_id, (detail or "").strip()[:300], aid, org_id),
        )
        return cur.rowcount > 0


# Execution states the orchestrator may report — the brain's lifecycle for an
# action it received (proposal card up → human decision → tool run outcome).
EXECUTION_STATUSES = ("proposed", "approved", "rejected", "done", "failed")


# Terminal execution statuses close the ledger item with the matching resolve
# outcome — one weld point so the provenance channel (/status, what Cedric's
# own loop reports) and the closure channel (/resolve) can never disagree.
# 'proposed'/'approved' are in-flight and must NOT close anything.
_TERMINAL_STATUS_OUTCOME = {"done": "done", "rejected": "rejected", "failed": "failed"}


def set_action_status(action_id: str, status: str, detail: str = "") -> bool:
    """Record the orchestrator-reported execution state of an action (upsert,
    latest wins). Terminal statuses (done/rejected/failed) also close the
    ledger item with the matching outcome — same effect as the resolve
    endpoint — so the two reporting paths can't disagree (live gap 2026-07-10:
    Cedric's status loop reported 'rejected' but the ledger row stayed open
    forever). Unknown status or empty id is a no-op (False). ``detail`` is a
    distilled one-liner (card link, error class); it is capped, and it is
    never transcript content by contract."""
    aid = (action_id or "").strip()
    st = (status or "").strip().lower()
    if not aid or st not in EXECUTION_STATUSES:
        return False
    with store._LOCK, store._connect() as conn:
        conn.execute(
            """INSERT INTO action_status (action_id, status, detail, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(action_id) DO UPDATE SET
                 status=excluded.status, detail=excluded.detail,
                 updated_at=excluded.updated_at""",
            (aid, st, (detail or "").strip()[:300], time.time()),
        )
    outcome = _TERMINAL_STATUS_OUTCOME.get(st)
    if outcome:
        resolve_by_action_id(aid, "", outcome, (detail or "").strip()[:300])
    return True


def action_statuses(action_ids: list[str]) -> dict[str, dict]:
    """Latest execution state for each of the given action_ids (missing ids
    simply absent). One query — the dashboard decorates a page of meetings."""
    ids = [a for a in {(i or "").strip() for i in action_ids} if a]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    with store._LOCK, store._connect() as conn:
        rows = conn.execute(
            f"""SELECT action_id, status, detail, updated_at
                FROM action_status WHERE action_id IN ({marks})""",
            ids,
        ).fetchall()
    return {
        r["action_id"]: {
            "status": r["status"],
            "detail": r["detail"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    }


def carryover_brief(
    meeting_url: str, *, limit: int = 8, org_id: str = DEMO_ORG_ID
) -> str:
    """Compact 'what previous meetings left open' block for prompt injection
    and pre-meeting briefs. Scoped to ``org_id`` — this feeds the LIVE prompt,
    so another tenant's open items must never surface mid-meeting (§6.4). Empty
    string when there is no history — callers can skip the block entirely."""
    key = meeting_key(meeting_url)
    with store._LOCK, store._connect() as conn:
        open_rows = conn.execute(
            """SELECT kind, item, owner, deadline, created_at FROM ledger_items
               WHERE org_id=? AND meeting_key=? AND status='open'
               ORDER BY id LIMIT ?""",
            (org_id, key, limit),
        ).fetchall()
        decisions = conn.execute(
            """SELECT item FROM ledger_items
               WHERE org_id=? AND meeting_key=? AND kind='decision'
               ORDER BY id DESC LIMIT 3""",
            (org_id, key),
        ).fetchall()
    if not open_rows and not decisions:
        return ""

    lines: list[str] = []
    steps = [r for r in open_rows if r["kind"] == "missing_step"]
    actions = [r for r in open_rows if r["kind"] == "action"]
    if steps:
        lines.append(
            "Process steps still not confirmed from previous meetings: "
            + ", ".join(
                f"{humanize_step(r['item'])} (open since {_day(r['created_at'])})"
                for r in steps
            )
            + "."
        )
    if actions:
        lines.append("Open action items from previous meetings:")
        for r in actions:
            owner = f" (owner: {r['owner']})" if r["owner"] and r["owner"] != "UNASSIGNED" else ""
            due = f" (due {r['deadline']})" if r["deadline"] else ""
            lines.append(f"- {r['item']}{owner}{due}")
    if decisions:
        lines.append(
            "Decisions already made: " + "; ".join(r["item"] for r in decisions) + "."
        )
    return "\n".join(lines)


def _day(ts: float) -> str:
    return time.strftime("%b %d", time.localtime(ts))


_init_db()
