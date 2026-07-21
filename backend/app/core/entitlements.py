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

Role/RLS contract: the runtime is always ``laura_app`` (NOSUPERUSER and
NOBYPASSRLS). Every per-bot mutation/read requires its caller's org_id, sets
transaction-local ``app.current_org`` before the first query, and includes
org_id in the predicate. The only cross-tenant operation is restart recovery's
read-only list of pending/active rows; it uses the narrow
``laura_private.list_open_usage_sessions`` definer function.

Nothing here ever logs transcripts, emails, tokens, or any PII.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .config import settings
from .. import control_plane


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
    """Consumption in the current paid period, or lifetime for the free plan."""
    from sqlalchemy import text

    meta = conn.execute(
        text(
            "SELECT plan, EXTRACT(EPOCH FROM current_period_start) "
            "FROM billing_accounts WHERE org_id = :o"
        ),
        {"o": org_id},
    ).fetchone()
    windowed = bool(
        meta
        and str(meta[0]) == "solo"
        and meta[1] is not None
    )
    if windowed:
        closed = conn.execute(
            text(
                "SELECT COALESCE(SUM(LEAST("
                "  consumed_seconds, "
                "  GREATEST(0, EXTRACT(EPOCH FROM ("
                "    closed_at - "
                "    to_timestamp(CAST(:period_start AS double precision))"
                "  )))"
                ")), 0) "
                "FROM usage_sessions "
                "WHERE org_id = :o AND state = 'closed' "
                "AND closed_at >= "
                "to_timestamp(CAST(:period_start AS double precision))"
            ),
            {"o": org_id, "period_start": float(meta[1])},
        ).scalar()
    else:
        closed = conn.execute(
            text(
                "SELECT COALESCE(SUM(consumed_seconds), 0) "
                "FROM usage_sessions "
                "WHERE org_id = :o AND state = 'closed'"
            ),
            {"o": org_id},
        ).scalar()
    if windowed:
        active = conn.execute(
            text(
                "SELECT COALESCE(SUM(GREATEST(0, EXTRACT(EPOCH FROM ("
                "  now() - GREATEST("
                "    in_call_at, "
                "    to_timestamp(CAST(:period_start AS double precision))"
                "  )"
                ")))), 0) "
                "FROM usage_sessions "
                "WHERE org_id = :o AND state = 'active' "
                "AND in_call_at IS NOT NULL AND bot_id <> :skip"
            ),
            {
                "o": org_id,
                "skip": exclude_bot_id or "",
                "period_start": float(meta[1]),
            },
        ).scalar()
    else:
        active = conn.execute(
            text(
                "SELECT COALESCE(SUM(GREATEST(0, "
                "  EXTRACT(EPOCH FROM (now() - in_call_at)))), 0) "
                "FROM usage_sessions "
                "WHERE org_id = :o AND state = 'active' "
                "AND in_call_at IS NOT NULL AND bot_id <> :skip"
            ),
            {"o": org_id, "skip": exclude_bot_id or ""},
        ).scalar()
    return float(closed or 0) + float(active or 0)


# Comped orgs (plan='comp'): effectively unlimited — ~31 years of seconds.
# A sentinel this large keeps every "remaining > 0" gate trivially true
# without a special case at each call site.
_COMP_ALLOWANCE = 10**9


def is_comp_email(email: str) -> bool:
    """True when this email is on the comped list (BILLING_COMP_EMAILS):
    full access, billing waived. Matched case-insensitively, exact only."""
    listed = {
        e.strip().lower()
        for e in settings.billing_comp_emails.split(",")
        if e.strip()
    }
    return bool(email) and email.strip().lower() in listed


def grant_comp(org_id: str) -> bool:
    """Waive billing for an org: upsert its billing row to plan='comp' with
    the unlimited allowance. Called at login for comped emails — best-effort
    and idempotent; a clean no-op (False) when the control plane is off (no
    metering means nothing to waive). Never raises: a billing hiccup must
    never break a login."""
    if not enabled():
        return False
    org = (org_id or "").strip()
    if not org:
        return False
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        with _engine().begin() as conn:
            control_plane._set_org(conn, org)
            conn.execute(
                text(
                    "INSERT INTO billing_accounts (org_id, plan, included_seconds) "
                    "VALUES (:o, 'comp', :s) "
                    "ON CONFLICT (org_id) DO UPDATE SET "
                    "plan = 'comp', included_seconds = :s"
                ),
                {"o": org, "s": _COMP_ALLOWANCE},
            )
        return True
    except SQLAlchemyError:
        return False


def _effective_allowance(row) -> int:
    """Paid access is valid only through its verified Stripe period end."""
    if row is None:
        return _included_default()
    included = int(row[0])
    plan = str(row[1] or "free")
    if plan == "comp":
        # Comped (BILLING_COMP_EMAILS): billing waived, allowance unlimited.
        return max(included, _COMP_ALLOWANCE)
    if plan != "solo":
        return included
    status = str(row[2] or "none")
    period_end = float(row[3]) if row[3] is not None else None
    period_start = float(row[4]) if row[4] is not None else None
    if (
        status not in {"active", "past_due"}
        or period_start is None
        or period_end is None
        or period_end <= time.time()
    ):
        return 0
    return included


def _lock_billing_row(conn, org_id: str) -> int:
    """included_seconds for the org, row LOCKED (FOR UPDATE) so concurrent
    gates/deadline computations serialize. Creates the free-plan row when
    missing (an org that signed up before billing provisioning existed)."""
    from sqlalchemy import text

    row = conn.execute(
        text(
            "SELECT included_seconds, plan, subscription_status, "
            "EXTRACT(EPOCH FROM current_period_end), "
            "EXTRACT(EPOCH FROM current_period_start) "
            "FROM billing_accounts "
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
                "SELECT included_seconds, plan, subscription_status, "
                "EXTRACT(EPOCH FROM current_period_end), "
                "EXTRACT(EPOCH FROM current_period_start) "
                "FROM billing_accounts "
                "WHERE org_id = :o FOR UPDATE"
            ),
            {"o": org_id},
        ).fetchone()
    return _effective_allowance(row)


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
                text(
                    "SELECT included_seconds, plan, subscription_status, "
                    "EXTRACT(EPOCH FROM current_period_end), "
                    "EXTRACT(EPOCH FROM current_period_start) "
                    "FROM billing_accounts WHERE org_id = :o"
                ),
                {"o": org},
            ).fetchone()
            included = _effective_allowance(row)
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


def assign_bot_id(org_id: str, provisional_bot_id: str, bot_id: str) -> bool:
    """Swap a provisional id for the real Recall bot inside one tenant.

    False means no row changed and MUST NOT be treated as successful binding.
    """
    if not enabled():
        return False
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    org = (org_id or "").strip()
    provisional = (provisional_bot_id or "").strip()
    real = (bot_id or "").strip()
    if not org or not provisional or not real:
        return False
    try:
        with _engine().begin() as conn:
            control_plane._set_org(conn, org)
            res = conn.execute(
                text(
                    "UPDATE usage_sessions SET bot_id = :real "
                    "WHERE org_id = :o AND bot_id = :prov AND state != 'closed'"
                ),
                {"o": org, "real": real, "prov": provisional},
            )
            return bool(res.rowcount)
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc


def mark_in_call(
    org_id: str, bot_id: str, in_call_at_epoch: float
) -> Optional[float]:
    """Start one org's Recall-status clock and return its hard deadline.

    The org is supplied by the authenticated/local session or durable restart
    row; it is never discovered with a global bot_id lookup.
    """
    if not enabled():
        return None
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    org = (org_id or "").strip()
    bot = (bot_id or "").strip()
    if not org or not bot:
        return None
    try:
        with _engine().begin() as conn:
            control_plane._set_org(conn, org)
            peek = conn.execute(
                text(
                    "SELECT state, EXTRACT(EPOCH FROM in_call_at), "
                    "EXTRACT(EPOCH FROM deadline) FROM usage_sessions "
                    "WHERE org_id = :o AND bot_id = :b"
                ),
                {"o": org, "b": bot},
            ).fetchone()
            if peek is None or str(peek[0]) == "closed":
                return None
            if peek[1] is not None:
                return float(peek[2]) if peek[2] is not None else None

            included = _lock_billing_row(conn, org)
            row = conn.execute(
                text(
                    "SELECT id, EXTRACT(EPOCH FROM in_call_at), "
                    "EXTRACT(EPOCH FROM deadline) FROM usage_sessions "
                    "WHERE org_id = :o AND bot_id = :b AND state != 'closed' "
                    "FOR UPDATE"
                ),
                {"o": org, "b": bot},
            ).fetchone()
            if row is None:
                return None
            if row[1] is not None:
                return float(row[2]) if row[2] is not None else None
            remaining = max(
                0.0, included - _used_seconds(conn, org, exclude_bot_id=bot)
            )
            deadline = float(in_call_at_epoch) + remaining
            conn.execute(
                text(
                    "UPDATE usage_sessions SET state = 'active', "
                    "in_call_at = to_timestamp(:t), deadline = to_timestamp(:d) "
                    "WHERE org_id = :o AND id = :id"
                ),
                {
                    "o": org,
                    "t": float(in_call_at_epoch),
                    "d": deadline,
                    "id": row[0],
                },
            )
            return deadline
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc


def close_usage(
    org_id: str, bot_id: str, consumed_seconds: int, reason: str
) -> bool:
    """Finalize one tenant's row, idempotently (first close wins)."""
    if not enabled():
        return False
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    org = (org_id or "").strip()
    bot = (bot_id or "").strip()
    if not org or not bot:
        return False
    try:
        with _engine().begin() as conn:
            control_plane._set_org(conn, org)
            res = conn.execute(
                text(
                    "UPDATE usage_sessions SET state = 'closed', "
                    "consumed_seconds = CASE WHEN in_call_at IS NULL "
                    "  THEN 0 ELSE :c END, "
                    "closed_at = now(), close_reason = :r "
                    "WHERE org_id = :o AND bot_id = :b AND state != 'closed'"
                ),
                {
                    "o": org,
                    "c": max(0, int(consumed_seconds or 0)),
                    "r": (reason or "")[:80],
                    "b": bot,
                },
            )
            return bool(res.rowcount)
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc


def usage_row(org_id: str, bot_id: str) -> Optional[dict]:
    """One usage row, visible only inside the supplied org."""
    if not enabled():
        return None
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    org = (org_id or "").strip()
    bot = (bot_id or "").strip()
    if not org or not bot:
        return None
    try:
        with _engine().begin() as conn:
            control_plane._set_org(conn, org)
            row = conn.execute(
                text(
                    "SELECT state, EXTRACT(EPOCH FROM in_call_at), "
                    "EXTRACT(EPOCH FROM deadline), consumed_seconds, close_reason "
                    "FROM usage_sessions WHERE org_id = :o AND bot_id = :b"
                ),
                {"o": org, "b": bot},
            ).fetchone()
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc
    if row is None:
        return None
    return {
        "org_id": org,
        "bot_id": bot,
        "state": str(row[0]),
        "in_call_at": float(row[1]) if row[1] is not None else None,
        "deadline": float(row[2]) if row[2] is not None else None,
        "consumed_seconds": int(row[3] or 0),
        "close_reason": str(row[4] or ""),
    }


def open_usage_rows() -> list[dict[str, Any]]:
    """Read-only global restart inventory through the exact private RPC."""
    if not enabled():
        return []
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT org_id, bot_id, avatar_id, state, created_at_epoch, "
                    "in_call_at_epoch, deadline_epoch "
                    "FROM laura_private.list_open_usage_sessions()"
                )
            ).fetchall()
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc
    return [
        {
            "org_id": str(row[0]),
            "bot_id": str(row[1]),
            "avatar_id": str(row[2] or ""),
            "state": str(row[3]),
            "created_at": float(row[4]) if row[4] is not None else time.time(),
            "in_call_at": float(row[5]) if row[5] is not None else None,
            "deadline": float(row[6]) if row[6] is not None else None,
        }
        for row in rows
    ]


def usage_summary(org_id: str) -> Optional[dict]:
    """``{plan, included_seconds, used_seconds, remaining_seconds}`` for the
    org — PR C's /billing/summary calls this. ``None`` when disabled."""
    if not enabled() or not (org_id or "").strip():
        return None
    # A session-shaped personal identity (u_<hash>) has no durable billing row
    # and would crash the RLS org_id uuid cast — a 500 on /billing/summary.
    # Degrade to the free-tier defaults instead (see control_plane.is_durable_org).
    if not control_plane._is_uuid(org_id.strip()):
        return None
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    org = org_id.strip()
    try:
        with _engine().begin() as conn:
            control_plane._set_org(conn, org)
            row = conn.execute(
                text(
                    "SELECT plan, included_seconds, subscription_status, "
                    "EXTRACT(EPOCH FROM current_period_end), "
                    "EXTRACT(EPOCH FROM current_period_start) "
                    "FROM billing_accounts "
                    "WHERE org_id = :o"
                ),
                {"o": org},
            ).fetchone()
            plan = str(row[0]) if row else "free"
            included = int(row[1]) if row else _included_default()
            effective = _effective_allowance(
                (row[1], row[0], row[2], row[3], row[4])
                if row else None
            )
            used = int(_used_seconds(conn, org))
    except SQLAlchemyError as e:
        raise _unavailable(e) from e
    return {
        "plan": plan,
        "included_seconds": included,
        "used_seconds": used,
        "remaining_seconds": max(0, effective - used),
    }


def has_active_session(org_id: str) -> bool:
    """Whether this org's one meeting slot is currently occupied."""
    if not enabled() or not (org_id or "").strip():
        return False
    # Personal (u_<hash>) orgs have no usage_sessions rows and would only burn
    # a doomed uuid-cast query (caught below, but noisy) — answer directly.
    if not control_plane._is_uuid(org_id.strip()):
        return False
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    org = org_id.strip()
    try:
        with _engine().begin() as conn:
            control_plane._set_org(conn, org)
            return conn.execute(
                text(
                    "SELECT 1 FROM usage_sessions "
                    "WHERE org_id = :o AND state IN ('pending', 'active') "
                    "LIMIT 1"
                ),
                {"o": org},
            ).fetchone() is not None
    except SQLAlchemyError:
        return False
