"""Native executor — turn an APPROVED ledger action into a real vendor call.

The seam from NATIVE-INTEGRATIONS-PLAN.md ("Now" slice): when
``settings.native_executor`` is ON, an approved calendar/gmail/asana action is
executed directly on the org's own account (``google_client`` /
``asana_client``) instead of being brokered through Cedric, and the outcome is
written back to the SAME ledger provenance channel the dashboard already reads
(``ledger.set_action_status`` → "done"/"failed", closing the ledger row and
carrying the event link / message id / task URL as the receipt). With the flag
OFF this module does nothing and the Cedric path is byte-identical.

This runs at finalize/approval, NEVER on the transcript→speak live path, and it
never raises: any failure becomes a soft "failed" receipt.

Action shape (the caller — dashboard approve / finalize — supplies it; the
free-text ledger row is only the correlation via ``action_id``):
    {"type": "calendar.create_event", "event":   {title, start, end, attendees?, ...}}
    {"type": "email.send",            "message": {to, subject, body}}
    {"type": "asana.create_task",     "task":    {name, notes?, project?, assignee?, due_on?}}
    {"type": "asana.update_task",     "task":    {task (gid), completed?, due_on?, ...}}
    {"type": "asana.add_comment",     "task":    {task (gid), text}}
Fields may also be inlined alongside ``type`` instead of nested.
"""
from __future__ import annotations

from . import asana_client, google_client, ledger
from .config import settings

CALENDAR_CREATE = "calendar.create_event"
EMAIL_SEND = "email.send"
ASANA_CREATE = "asana.create_task"
ASANA_UPDATE = "asana.update_task"
ASANA_COMMENT = "asana.add_comment"
ASANA_ACTION_TYPES = frozenset({ASANA_CREATE, ASANA_UPDATE, ASANA_COMMENT})
NATIVE_ACTION_TYPES = frozenset({CALENDAR_CREATE, EMAIL_SEND}) | ASANA_ACTION_TYPES

# Which per-avatar capability toggle governs an action type — the approve
# seam skips execution on an explicit False for the action's OWN family
# (an avatar with Google off can still push Asana tasks, and vice versa).
_CAPABILITY_FAMILY = {
    CALENDAR_CREATE: "google",
    EMAIL_SEND: "google",
    ASANA_CREATE: "asana",
    ASANA_UPDATE: "asana",
    ASANA_COMMENT: "asana",
}


def capability_family(action_type: str | None) -> str:
    """The capability toggle key for an action type ("google" default)."""
    return _CAPABILITY_FAMILY.get(str(action_type or ""), "google")


def from_typed(typed: dict | None) -> dict | None:
    """Bridge a producer typed spec ``{type, args}`` to this module's action
    shape, or None for a missing/non-native spec (the signal to approve
    without executing). Shared by the dashboard approve door and the
    finalize auto-push so the two callers can never disagree."""
    if not isinstance(typed, dict):
        return None
    t = str(typed.get("type") or "")
    args = typed.get("args") if isinstance(typed.get("args"), dict) else {}
    if t == CALENDAR_CREATE:
        return {"type": t, "event": args}
    if t == EMAIL_SEND:
        return {"type": t, "message": args}
    if t in ASANA_ACTION_TYPES:
        return {"type": t, "task": args}
    return None


def enabled() -> bool:
    """Whether native execution is active (the flag is the single switch)."""
    return bool(settings.native_executor)


def handles(action: dict | None) -> bool:
    """True when native execution is on AND this action is one we execute — the
    branch the finalize/approval path uses to choose native vs Cedric."""
    return enabled() and (action or {}).get("type") in NATIVE_ACTION_TYPES


def execute_approved(org_id: str, action_id: str, action: dict) -> dict:
    """Execute one approved action and record its outcome on the ledger.

    Returns the ``google_client`` result (``{"ok": bool, ...}``) or a
    ``{"ok": False, "skipped": ...}`` when native execution doesn't apply. Never
    raises."""
    if not enabled():
        return {"ok": False, "skipped": "native_executor off"}
    atype = (action or {}).get("type")
    if atype not in NATIVE_ACTION_TYPES:
        return {"ok": False, "skipped": f"unhandled action type {atype!r}"}
    org = (org_id or "").strip()
    if not org:
        return {"ok": False, "error": "missing org"}

    try:
        if atype == CALENDAR_CREATE:
            result = google_client.create_calendar_event(
                org, action.get("event") or action
            )
            what, receipt = "calendar event", (
                result.get("event_url") or result.get("event_id") or ""
            )
        elif atype == EMAIL_SEND:
            result = google_client.send_gmail(org, action.get("message") or action)
            what, receipt = "email", result.get("message_id") or ""
        elif atype == ASANA_CREATE:
            result = asana_client.create_task(org, action.get("task") or action)
            what, receipt = "asana task", (
                result.get("task_url") or result.get("task_gid") or ""
            )
        elif atype == ASANA_UPDATE:
            result = asana_client.update_task(org, action.get("task") or action)
            what, receipt = "asana task update", (
                result.get("task_url") or result.get("task_gid") or ""
            )
        else:  # ASANA_COMMENT
            result = asana_client.add_comment(org, action.get("task") or action)
            what, receipt = "asana comment", (
                result.get("task_url") or result.get("task_gid") or ""
            )
    except Exception as e:  # noqa: BLE001 — clients already soft-return; belt+braces
        result = {"ok": False, "error": f"executor error ({type(e).__name__})"}
        what, receipt = atype, ""

    # Write provenance back through the existing status weld point: "done" closes
    # the ledger row and shows a receipt in the dashboard; "failed" surfaces why.
    aid = (action_id or "").strip()
    if aid:
        try:
            if result.get("ok"):
                detail = " · ".join(p for p in ("native", what, receipt) if p)[:300]
                ledger.set_action_status(
                    aid, "done", detail, org_id=org,
                    receipt={"kind": what, "ref": receipt, "route": "native"},
                )
            else:
                detail = f"native · {result.get('error', 'failed')}"[:300]
                ledger.set_action_status(aid, "failed", detail, org_id=org)
        except Exception as e:  # noqa: BLE001 — provenance is best-effort
            print(
                f"[executor] status write skipped ({type(e).__name__})", flush=True
            )
        # handshake operation action-events: mirror the NATIVE-route terminal
        # status to the orchestrator so Slack cards + dashboard stay in sync
        # (contract: native-route surfacing — a silent native outcome is a
        # violation on either route). A-authored → fresh event_id is minted in
        # send_action_event; best-effort, orgs without a Cedric link no-op.
        try:
            from .cedric import callback as cedric_callback

            cedric_callback.send_action_event(org, "action.status", {
                "action_id": aid,
                "status": "done" if result.get("ok") else "failed",
                "detail": detail[:300],
                "receipt_url": receipt if result.get("ok") else "",
            })
        except Exception:  # noqa: BLE001 — mirroring must never break execution
            pass
    return result


def auto_execute_asana(org_id: str, actions: list) -> int:
    """Asana auto-push (settings.asana_auto_execute): execute every typed
    asana.* action in an artifact's action list RIGHT AT FINALIZE, without
    waiting for dashboard approval. Returns how many were attempted.

    Only Asana types auto-push — calendar/email keep the human approval step
    regardless (an email to a customer is not the same blast radius as a task
    in the team's own board). Receipts land in the same ledger provenance
    channel as approved runs, so the dashboard rows show Done ↗ / failed
    exactly as if a human had clicked. Best-effort, never raises: finalize
    (the meter stop) must never be blocked by a task push."""
    if not (enabled() and settings.asana_auto_execute):
        return 0
    attempted = 0
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        action = from_typed(a.get("typed"))
        if action is None or action.get("type") not in ASANA_ACTION_TYPES:
            continue
        aid = str(a.get("action_id") or "")
        if not aid:
            continue
        try:
            execute_approved(org_id, aid, action)
            attempted += 1
        except Exception:  # noqa: BLE001 — belt+braces; execute_approved soft-returns
            continue
    return attempted
