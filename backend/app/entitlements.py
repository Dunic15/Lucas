"""Usage metering + the 15-minute free entitlement (PR B of the self-serve flow).

The durable Postgres ledger of avatar-minutes per org, layered on PR A's
control plane (same engine, same enabled() switch, same role contract). Active
**iff** ``settings.laura_database_url`` is non-empty; when it is empty — the
key-free demo and the whole offline test suite — every public function here is
a no-op returning ``None``/``[]`` and **no engine is ever created**, so the
demo stays byte-identical to today.

Model (alembic 0003, ``usage_sessions``):

  pending  → the atomic gate passed and a bot is being dispatched. Consumes 0.
  active   → Recall reported an in-call status. ``in_call_at`` is Recall's own
             timestamp (authoritative — the meter is what Recall bills, not
             what our process observed); ``deadline`` = in_call_at + the org's
             remaining seconds AT THAT MOMENT.
  closed   → final. ``consumed_seconds`` is written once (first close wins;
             retries can never rewrite it) and ``close_reason`` says why.

remaining = billing_accounts.included_seconds
            - SUM(consumed_seconds of closed rows)
            - elapsed(now - in_call_at) of any ACTIVE row   (pending rows: 0)

Concurrency: ``open_usage`` is THE gate — one transaction that locks the
billing row (``FOR UPDATE``), computes remaining inside the lock, and inserts
the pending row. Two racing starts serialize on the row lock, and the partial
unique index ``uq_usage_one_active_per_org`` (one pending/active row per org)
makes the loser fail with a unique violation instead of an overspend — the
database enforces "one concurrent meeting per org" even across processes.

Latency: NOTHING here runs on the transcript→token hot path. The gate runs at
session start, the clock at the ~60s reconcile pass — both via
``run_in_threadpool`` (sync engine) so the event loop never blocks.

Role/RLS contract: identical to control_plane's (see its module docstring).
Org-scoped operations SET ``app.current_org`` so RLS is exercised; bot_id
lookups (``mark_in_call``/``close_usage``/``open_usage_rows``) are inherently
cross-tenant — the org is the *answer* — and rely on the owner/BYPASSRLS role
the control plane already requires. RLS remains the safety net for any
lower-privileged role.

Nothing here ever logs transcripts, emails, tokens, or any PII.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .config import settings
from . import control_plane


class EntitlementsUnavailable(RuntimeError):
    """The billing database is configured but unreachable/broken. Callers must
    fail CLOSED for new paid dispatches (503 before any vendor call) — never
    silently grant free minutes."""


class UsageDenied(RuntimeError):
    """The atomic gate refused the start. ``reason`` is one of
    ``usage_limit_reached`` | ``active_session_exists``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def enabled() -> bool:
    """Metering is on iff the control-plane database is configured."""
    return control_plane.enabled()


def _engine():
    return control_plane._get_engine()


def _included_default() -> int:
    return int(settings.free_trial_seconds)


# ── internal SQL helpers (all run inside a caller-owned connection) ─────

def _used_seconds(conn, org_id: str, *, exclude_bot_id: str | None = None) -> float:
    """closed consumption + live elapsed of active rows (pending rows count 0).
    ``exclude_bot_id`` leaves one row's OWN elapsed out (mark_in_call computes
    the deadline for that row, so it must not count against itself)."""
    from sqlalchemy import text

    closed = conn.execute(
        text(
            "SELECT COALESCE(SUM(consumed_seconds), 0) FROM usage_sessions "
            "WHERE org_id = :o AND state = 'closed'"
        ),
        {"o": org_id},
    ).scalar()
    active = conn.execute(
        text(
            "SELECT COALESCE(SUM(GREATEST(0, "
            "  EXTRACT(EPOCH FROM (now() - in_call_at)))), 0) "
            "FROM usage_sessions "
            "WHERE org_id = :o AND state = 'active' AND in_call_at IS NOT NULL "
            "AND bot_id <> :skip"
        ),
        {"o": org_id, "skip": exclude_bot_id or ""},
    ).scalar()
    return float(closed or 0) + float(active or 0)


def _lock_billing_row(conn, org_id: str) -> int:
    """included_seconds for the org, row LOCKED (FOR UPDATE) so concurrent
    gates/deadline computations serialize. Creates the free-plan row when
    missing (an org that signed up before billing provisioning existed)."""
    from sqlalchemy import text

    row = conn.execute(
        text(
            "SELECT included_seconds FROM billing_accounts "
            "WHERE org_id = :o FOR UPDATE"
        ),
        {"o": org_id},
    ).fetchone()
    if row is None:
        conn.execute(
            text(
                "INSERT INTO billing_accounts (org_id, plan, included_seconds) "
                "VALUES (:o, 'free', :inc) ON CONFLICT (org_id) DO NOTHING"
            ),
            {"o": org_id, "inc": _included_default()},
        )
        # Re-read under the lock: on a conflict the WINNER's row is what counts.
        row = conn.execute(
            text(
                "SELECT included_seconds FROM billing_accounts "
                "WHERE org_id = :o FOR UPDATE"
            ),
            {"o": org_id},
        ).fetchone()
    return int(row[0]) if row else _included_default()


def _unavailable(exc: Exception) -> EntitlementsUnavailable:
    # Never include SQL parameters in the surfaced error (no org ids leak to
    # HTTP bodies); the class name is enough to diagnose the outage class.
    return EntitlementsUnavailable(type(exc).__name__)


# ── public API ──────────────────────────────────────────────────────────

def remaining_seconds(org_id: str) -> Optional[int]:
    """Seconds of included avatar time this org still has (clamped >= 0), or
    ``None`` when the control plane is disabled. Counts closed consumption plus
    the LIVE elapsed of any active meeting, so a meeting in progress already
    draws down the balance."""
    if not enabled() or not (org_id or "").strip():
        return None
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    org = org_id.strip()
    try:
        with _engine().begin() as conn:
            control_plane._set_org(conn, org)
            row = conn.execute(
                text("SELECT included_seconds FROM billing_accounts WHERE org_id = :o"),
                {"o": org},
            ).fetchone()
            included = int(row[0]) if row else _included_default()
            used = _used_seconds(conn, org)
    except SQLAlchemyError as e:
        raise _unavailable(e) from e
    return max(0, int(included - used))


def open_usage(org_id: str, bot_id: str, avatar_id: str = "") -> Optional[dict]:
    """THE ATOMIC GATE — call BEFORE dispatching a paid bot. One transaction:

    1. lock the org's billing row (FOR UPDATE; created free/900 if missing),
    2. compute remaining INSIDE the lock,
    3. remaining <= 0 → ``{"ok": False, "reason": "usage_limit_reached"}``,
    4. INSERT the 'pending' usage row — a unique violation on the partial
       index (a concurrent pending/active row for this org) →
       ``{"ok": False, "reason": "active_session_exists"}``.

    Success: ``{"ok": True, "usage_id", "remaining_seconds"}``. ``None`` when
    the control plane is disabled (key-free demo — no enforcement). Any
    operational DB error raises :class:`EntitlementsUnavailable` — callers
    must refuse the dispatch (fail closed), never treat it as a free pass.
    """
    if not enabled():
        return None
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError

    org = (org_id or "").strip()
    bot = (bot_id or "").strip()
    if not org or not bot:
        raise ValueError("open_usage requires org_id and bot_id")
    try:
        # NOTE: the IntegrityError must propagate OUT of the ``begin()`` block
        # (clean rollback) before being classified — catching it inside would
        # let the context manager COMMIT an aborted transaction.
        with _engine().begin() as conn:
            control_plane._set_org(conn, org)
            included = _lock_billing_row(conn, org)
            remaining = included - _used_seconds(conn, org)
            if remaining <= 0:
                return {"ok": False, "reason": "usage_limit_reached"}
            row = conn.execute(
                text(
                    "INSERT INTO usage_sessions "
                    "(org_id, bot_id, avatar_id, state) "
                    "VALUES (:o, :b, :a, 'pending') RETURNING id"
                ),
                {"o": org, "b": bot, "a": (avatar_id or "").strip()},
            ).fetchone()
            return {
                "ok": True,
                "usage_id": str(row[0]),
                "remaining_seconds": max(0, int(remaining)),
            }
    except IntegrityError:
        # The partial unique index uq_usage_one_active_per_org: someone else's
        # pending/active row (or a duplicate bot_id). One meeting per org.
        return {"ok": False, "reason": "active_session_exists"}
    except SQLAlchemyError as e:
        raise _unavailable(e) from e


def assign_bot_id(provisional_bot_id: str, bot_id: str) -> bool:
    """Swap the gate's provisional id ('pending:<uuid>') for the real Recall
    bot id right after create_bot returns (both inside the meeting lock).
    Returns whether a row was updated. Never touches closed rows."""
    if not enabled():
        return False
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        with _engine().begin() as conn:
            res = conn.execute(
                text(
                    "UPDATE usage_sessions SET bot_id = :real "
                    "WHERE bot_id = :prov AND state != 'closed'"
                ),
                {"real": (bot_id or "").strip(), "prov": (provisional_bot_id or "").strip()},
            )
            changed = bool(res.rowcount)
        return changed
    except SQLAlchemyError as e:
        raise _unavailable(e) from e


def mark_in_call(bot_id: str, in_call_at_epoch: float) -> Optional[float]:
    """The clock starts: Recall reported the bot in-call at
    ``in_call_at_epoch`` (Recall's OWN status timestamp — authoritative for
    what Recall bills). Sets state='active' and computes the hard deadline =
    in_call_at + the org's remaining seconds at this moment (recomputed under
    the billing-row lock, excluding this row's own elapsed).

    Idempotent: only the FIRST call (in_call_at IS NULL) writes; later calls
    return the already-set deadline. Returns the deadline epoch, or ``None``
    when disabled / row unknown / row closed.

    Lock order matches open_usage (billing row first, then the usage row) so
    a racing gate and a racing clock-start can never deadlock.
    """
    if not enabled():
        return None
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    bot = (bot_id or "").strip()
    if not bot:
        return None
    try:
        with _engine().begin() as conn:
            peek = conn.execute(
                text(
                    "SELECT org_id, state, "
                    "EXTRACT(EPOCH FROM in_call_at), EXTRACT(EPOCH FROM deadline) "
                    "FROM usage_sessions WHERE bot_id = :b"
                ),
                {"b": bot},
            ).fetchone()
            if peek is None or str(peek[1]) == "closed":
                return None
            org = str(peek[0])
            if peek[2] is not None:  # already marked — idempotent fast path
                return float(peek[3]) if peek[3] is not None else None
            control_plane._set_org(conn, org)
            included = _lock_billing_row(conn, org)  # billing lock FIRST
            row = conn.execute(
                text(
                    "SELECT id, EXTRACT(EPOCH FROM in_call_at), "
                    "EXTRACT(EPOCH FROM deadline) "
                    "FROM usage_sessions WHERE bot_id = :b AND state != 'closed' "
                    "FOR UPDATE"
                ),
                {"b": bot},
            ).fetchone()
            if row is None:
                return None
            if row[1] is not None:  # raced another marker — keep the first write
                return float(row[2]) if row[2] is not None else None
            remaining = max(
                0.0, included - _used_seconds(conn, org, exclude_bot_id=bot)
            )
            deadline = float(in_call_at_epoch) + remaining
            conn.execute(
                text(
                    "UPDATE usage_sessions SET state = 'active', "
                    "in_call_at = to_timestamp(:t), deadline = to_timestamp(:d) "
                    "WHERE id = :id"
                ),
                {"t": float(in_call_at_epoch), "d": deadline, "id": row[0]},
            )
            return deadline
    except SQLAlchemyError as e:
        raise _unavailable(e) from e


def close_usage(bot_id: str, consumed_seconds: int, reason: str) -> bool:
    """Finalize the row — IDEMPOTENT: ``WHERE state != 'closed'`` means the
    FIRST close wins and a retry (manual end + webhook + reconcile can all
    race) can never rewrite consumed_seconds to a different value. A row that
    never went in-call (failed/cancelled join) closes with consumed 0
    regardless of the passed value. Returns whether a row transitioned."""
    if not enabled():
        return False
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    bot = (bot_id or "").strip()
    if not bot:
        return False
    try:
        with _engine().begin() as conn:
            res = conn.execute(
                text(
                    "UPDATE usage_sessions SET state = 'closed', "
                    "consumed_seconds = CASE WHEN in_call_at IS NULL "
                    "  THEN 0 ELSE :c END, "
                    "closed_at = now(), close_reason = :r "
                    "WHERE bot_id = :b AND state != 'closed'"
                ),
                {
                    "c": max(0, int(consumed_seconds or 0)),
                    "r": (reason or "")[:80],
                    "b": bot,
                },
            )
            changed = bool(res.rowcount)
        return changed
    except SQLAlchemyError as e:
        raise _unavailable(e) from e


def usage_row(bot_id: str) -> Optional[dict]:
    """The one usage row for a bot (state / in_call_at / deadline as epochs),
    or ``None``. Finalize reads this to compute consumed_seconds."""
    if not enabled():
        return None
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    bot = (bot_id or "").strip()
    if not bot:
        return None
    try:
        with _engine().connect() as conn:
            row = conn.execute(
                text(
                    "SELECT org_id, state, EXTRACT(EPOCH FROM in_call_at), "
                    "EXTRACT(EPOCH FROM deadline), consumed_seconds, close_reason "
                    "FROM usage_sessions WHERE bot_id = :b"
                ),
                {"b": bot},
            ).fetchone()
    except SQLAlchemyError as e:
        raise _unavailable(e) from e
    if row is None:
        return None
    return {
        "org_id": str(row[0]),
        "bot_id": bot,
        "state": str(row[1]),
        "in_call_at": float(row[2]) if row[2] is not None else None,
        "deadline": float(row[3]) if row[3] is not None else None,
        "consumed_seconds": int(row[4] or 0),
        "close_reason": str(row[5] or ""),
    }


def open_usage_rows() -> list[dict[str, Any]]:
    """Every pending/active row — the restart-restore read: a row whose bot is
    NOT in the (ephemeral) local store is still enforced by the reconcile
    loop. Cross-tenant service read (owner role; see module docstring)."""
    if not enabled():
        return []
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT org_id, bot_id, avatar_id, state, "
                    "EXTRACT(EPOCH FROM created_at), "
                    "EXTRACT(EPOCH FROM in_call_at), EXTRACT(EPOCH FROM deadline) "
                    "FROM usage_sessions WHERE state IN ('pending', 'active')"
                )
            ).fetchall()
    except SQLAlchemyError as e:
        raise _unavailable(e) from e
    return [
        {
            "org_id": str(r[0]),
            "bot_id": str(r[1]),
            "avatar_id": str(r[2] or ""),
            "state": str(r[3]),
            "created_at": float(r[4]) if r[4] is not None else time.time(),
            "in_call_at": float(r[5]) if r[5] is not None else None,
            "deadline": float(r[6]) if r[6] is not None else None,
        }
        for r in rows
    ]


def usage_summary(org_id: str) -> Optional[dict]:
    """``{plan, included_seconds, used_seconds, remaining_seconds}`` for the
    org — PR C's /billing/summary calls this. ``None`` when disabled."""
    if not enabled() or not (org_id or "").strip():
        return None
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    org = org_id.strip()
    try:
        with _engine().begin() as conn:
            control_plane._set_org(conn, org)
            row = conn.execute(
                text(
                    "SELECT plan, included_seconds FROM billing_accounts "
                    "WHERE org_id = :o"
                ),
                {"o": org},
            ).fetchone()
            plan = str(row[0]) if row else "free"
            included = int(row[1]) if row else _included_default()
            used = int(_used_seconds(conn, org))
    except SQLAlchemyError as e:
        raise _unavailable(e) from e
    return {
        "plan": plan,
        "included_seconds": included,
        "used_seconds": used,
        "remaining_seconds": max(0, included - used),
    }
