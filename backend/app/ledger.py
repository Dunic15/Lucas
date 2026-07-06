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
import time
from typing import Any

from . import store
from .meeting_state import humanize_step

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


def _init_db() -> None:
    with store._LOCK, store._connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ledger_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_key TEXT NOT NULL,
                avatar_id TEXT NOT NULL,
                kind TEXT NOT NULL,          -- action | decision | missing_step
                item TEXT NOT NULL,          -- distilled line, never raw transcript
                owner TEXT NOT NULL DEFAULT '',
                deadline TEXT NOT NULL DEFAULT '',
                meeting_type TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',   -- open | done | noted
                bot_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                resolved_at REAL,
                resolved_by_bot_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_key_status
                ON ledger_items(meeting_key, status);
            """
        )


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def record_meeting(
    meeting_url: str, avatar_id: str, bot_id: str, artifact: dict[str, Any]
) -> dict[str, int]:
    """Fold one finished meeting's artifact into the ledger.

    - resolves previously-missing process steps that this meeting covered
      (same meeting_key + meeting_type, deterministic via the template),
    - inserts new open actions / missing steps / decisions,
    - dedupes on normalized item text per (meeting_key, kind).
    Returns counts for the caller's response payload.
    """
    key = meeting_key(meeting_url)
    meeting_type = str(artifact.get("meeting_type") or "")
    now = time.time()
    added = resolved = 0

    current_missing = {str(s) for s in artifact.get("missing_steps") or []}

    with store._LOCK, store._connect() as conn:
        # 1. Resolve: step was open from an earlier session of the same
        #    meeting+type, and this meeting no longer lists it as missing.
        if meeting_type:
            open_steps = conn.execute(
                """SELECT id, item FROM ledger_items
                   WHERE meeting_key=? AND kind='missing_step' AND status='open'
                     AND meeting_type=?""",
                (key, meeting_type),
            ).fetchall()
            for row in open_steps:
                if row["item"] not in current_missing:
                    conn.execute(
                        """UPDATE ledger_items
                           SET status='done', resolved_at=?, resolved_by_bot_id=?
                           WHERE id=?""",
                        (now, bot_id, row["id"]),
                    )
                    resolved += 1

        existing = {
            (row["kind"], _norm(row["item"]))
            for row in conn.execute(
                "SELECT kind, item FROM ledger_items WHERE meeting_key=?", (key,)
            ).fetchall()
        }

        def _insert(kind: str, item: str, owner: str = "", deadline: str = "", status: str = "open") -> None:
            nonlocal added
            item = (item or "").strip()[:200]
            if not item or (kind, _norm(item)) in existing:
                return
            conn.execute(
                """INSERT INTO ledger_items
                   (meeting_key, avatar_id, kind, item, owner, deadline,
                    meeting_type, status, bot_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, avatar_id, kind, item, owner, deadline, meeting_type, status, bot_id, now),
            )
            existing.add((kind, _norm(item)))
            added += 1

        for a in artifact.get("actions") or []:
            if isinstance(a, dict):
                _insert("action", a.get("item", ""), a.get("owner", "") or "",
                        a.get("deadline", "") or "")
            else:
                _insert("action", str(a))
        for step in current_missing:
            _insert("missing_step", step)
        for d in artifact.get("decisions") or []:
            _insert("decision", str(d), status="noted")

        # keep the ledger bounded per meeting
        conn.execute(
            """DELETE FROM ledger_items WHERE meeting_key=? AND id NOT IN (
                 SELECT id FROM ledger_items WHERE meeting_key=?
                 ORDER BY id DESC LIMIT ?)""",
            (key, key, _LIST_LIMIT),
        )

    return {"added": added, "resolved": resolved}


def items(meeting_key_: str, status: str = "") -> list[dict[str, Any]]:
    q = "SELECT * FROM ledger_items WHERE meeting_key=?"
    args: list[Any] = [meeting_key_]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY id"
    with store._LOCK, store._connect() as conn:
        return [dict(row) for row in conn.execute(q, args).fetchall()]


def resolve_item(item_id: int, bot_id: str = "") -> bool:
    with store._LOCK, store._connect() as conn:
        cur = conn.execute(
            """UPDATE ledger_items SET status='done', resolved_at=?, resolved_by_bot_id=?
               WHERE id=? AND status='open'""",
            (time.time(), bot_id, item_id),
        )
        return cur.rowcount > 0


def carryover_brief(meeting_url: str, *, limit: int = 8) -> str:
    """Compact 'what previous meetings left open' block for prompt injection
    and pre-meeting briefs. Empty string when there is no history — callers
    can skip the block entirely."""
    key = meeting_key(meeting_url)
    with store._LOCK, store._connect() as conn:
        open_rows = conn.execute(
            """SELECT kind, item, owner, deadline, created_at FROM ledger_items
               WHERE meeting_key=? AND status='open' ORDER BY id LIMIT ?""",
            (key, limit),
        ).fetchall()
        decisions = conn.execute(
            """SELECT item FROM ledger_items
               WHERE meeting_key=? AND kind='decision' ORDER BY id DESC LIMIT 3""",
            (key,),
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
