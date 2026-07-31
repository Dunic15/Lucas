"""Stale-``executing`` reconciler — the prerequisite for async dispatch.

An ``executing`` claim whose lease expired means the process died between the
claim and the receipt (or an ``ACTION_DISPATCH_ASYNC`` worker thread was
lost). The external write MAY have happened, so the one forbidden move is a
blind retry. This module settles such rows honestly:

- **calendar.create_event** — verified by READING the org's calendar around
  the intended slot (``google_client.list_calendar_events``): a matching
  event settles ``done`` with a real receipt; a definitive absence settles
  ``failed`` ("not created — re-capture to retry"); a read error waits.
- **everything else** (email/asana have no safe read-verify yet) — after a
  grace period the row settles ``failed`` with an explicit
  ``execution_unknown`` receipt telling the owner to check the connected
  account BEFORE retrying. Truthfully unknown beats forever-executing.

Runs lazily from org-scoped READS (dashboard summary, canonical action GET)
— never the live transcript path — throttled per org, so it needs no
cross-tenant discovery function and no scheduler. Terminal settles mirror to
Cedric through the same single-attempt event the executor uses.
"""
from __future__ import annotations

import threading
import time

from . import ledger
from .. import control_plane

# Per-org throttle: reads are frequent (15s dashboard refresh); one scan a
# minute per org is plenty for a crash-recovery path.
_TTL_SECONDS = 60.0
# Unverifiable claims settle only after this grace beyond the expired lease —
# generous headroom for a slow-but-alive vendor call plus mirror writes.
UNKNOWN_GRACE_SECONDS = 900.0

_LOCK = threading.Lock()
_LAST_SCAN: dict[str, float] = {}

_UNKNOWN_DETAIL = (
    "execution_unknown — a crash interrupted this call; check the connected "
    "account before retrying"
)


def _throttled(org_id: str) -> bool:
    now = time.time()
    with _LOCK:
        last = _LAST_SCAN.get(org_id, 0.0)
        if now - last < _TTL_SECONDS:
            return True
        _LAST_SCAN[org_id] = now
        return False


def _mirror(org_id: str, action_id: str, status: str, detail: str,
            receipt_url: str = "") -> None:
    """Best-effort Cedric mirror — identical discipline to the executor's."""
    try:
        from ..cedric import callback as cedric_callback

        cedric_callback.send_action_event(org_id, "action.status", {
            "action_id": action_id,
            "status": status,
            "detail": detail[:300],
            "receipt_url": receipt_url,
        })
    except Exception:  # noqa: BLE001 — mirroring must never break reconcile
        pass


def _settle(org_id: str, action_id: str, status: str, detail: str,
            receipt: dict | None) -> None:
    ledger.set_action_status(
        action_id, status, detail, org_id=org_id, receipt=receipt
    )
    _mirror(org_id, action_id, status, detail,
            str((receipt or {}).get("ref") or ""))


def _find_calendar_event(org_id: str, args: dict) -> tuple[str, str]:
    """('found', url) | ('absent', '') | ('error', '') for the intended event.

    Match rule (google_client.find_calendar_event — ONE implementation shared
    with update_calendar_event's resolver): same title (case/space-
    insensitive) with a start inside the intended day's window — deliberately
    narrow enough to avoid claiming an unrelated meeting as the receipt."""
    from ..integrations import google_client

    title = str(args.get("title") or "")
    start = str(args.get("start") or "").strip()
    if not title.strip() or not start:
        return "error", ""
    found = google_client.find_calendar_event(org_id, title, start[:10])
    if not found.get("ok"):
        return "error", ""
    matches = found.get("matches") or []
    if matches:
        m = matches[0]
        return "found", str(m.get("url") or m.get("id") or "")
    return "absent", ""


def _reconcile_row(org_id: str, row: dict) -> bool:
    """Settle one stale row when the evidence allows it. True when settled."""
    action_id = str(row.get("action_id") or "")
    typed = row.get("typed_json") if isinstance(row.get("typed_json"), dict) else {}
    args = typed.get("args") if isinstance(typed.get("args"), dict) else {}
    expired_for = float(row.get("expired_for") or 0)

    if typed.get("type") == "calendar.update_event":
        # Verify the reschedule landed: the event (by title) now starts at the
        # NEW time's day — found means the patch (or a manual move) happened.
        verdict, url = _find_calendar_event(org_id, args)
        if verdict == "found":
            _settle(
                org_id, action_id, "done",
                f"reconciled · calendar update · {url}"[:300],
                {"kind": "calendar update", "ref": url, "route": "native",
                 "reconciled": True},
            )
            return True
        # absent/error: the old event may simply still hold its old slot —
        # never claim failure from a narrow read; the grace settle applies.

    if typed.get("type") == "calendar.create_event":
        verdict, url = _find_calendar_event(org_id, args)
        if verdict == "found":
            _settle(
                org_id, action_id, "done",
                f"reconciled · calendar event · {url}"[:300],
                {"kind": "calendar event", "ref": url, "route": "native",
                 "reconciled": True},
            )
            return True
        if verdict == "absent":
            _settle(
                org_id, action_id, "failed",
                "execution_unknown resolved — the event was NOT created; "
                "re-capture to retry",
                {"kind": "unknown", "ref": "", "route": "native",
                 "reconciled": True},
            )
            return True
        # read error: keep waiting; the grace settle below still applies

    if expired_for >= UNKNOWN_GRACE_SECONDS:
        _settle(
            org_id, action_id, "failed", _UNKNOWN_DETAIL,
            {"kind": "unknown", "ref": "", "route": "native",
             "reconciled": True},
        )
        return True
    return False


def _reconcile_sqlite(org_id: str) -> int:
    """Key-free/per-instance twin: grace-settle stale executing rows in the
    SQLite action_status channel (no typed data to verify against here)."""
    from .. import store

    cutoff = time.time() - UNKNOWN_GRACE_SECONDS
    with store._LOCK, store._connect() as conn:
        rows = conn.execute(
            "SELECT action_id FROM action_status "
            "WHERE org_id=? AND status='executing' AND updated_at < ?",
            (org_id, cutoff),
        ).fetchall()
    settled = 0
    for row in rows:
        _settle(org_id, str(row["action_id"]), "failed", _UNKNOWN_DETAIL,
                None)
        settled += 1
    return settled


def maybe_reconcile(org_id: str) -> int:
    """Throttled per-org pass; returns how many rows were settled. Safe to
    call from any org-scoped read — it is sync DB I/O plus at most a bounded
    calendar read, and it never raises."""
    org = (org_id or "").strip()
    if not org:
        return 0
    if _throttled(org):
        return 0
    try:
        if not (control_plane.enabled() and control_plane.is_durable_org(org)):
            return _reconcile_sqlite(org)
        from . import outbox_pg

        settled = 0
        for row in outbox_pg.stale_executing(org):
            if _reconcile_row(org, row):
                settled += 1
        return settled
    except Exception as exc:  # noqa: BLE001 — a read path must never 500 on this
        print(
            f"[action-reconcile] pass failed: {type(exc).__name__}", flush=True
        )
        return 0


def _reset_for_tests() -> None:
    with _LOCK:
        _LAST_SCAN.clear()
