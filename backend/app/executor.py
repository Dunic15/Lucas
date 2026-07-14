"""Native executor — turn an APPROVED ledger action into a real Google call.

The new seam from NATIVE-INTEGRATIONS-PLAN.md ("Now" slice): when
``settings.native_executor`` is ON, an approved calendar/gmail action is executed
directly on the user's Google account (``google_client``) instead of being
brokered through Cedric, and the outcome is written back to the SAME ledger
provenance channel the dashboard already reads (``ledger.set_action_status`` →
"done"/"failed", closing the ledger row and carrying the event link / message id
as the receipt). With the flag OFF this module does nothing and the Cedric path
is byte-identical.

This runs at finalize/approval, NEVER on the transcript→speak live path, and it
never raises: any failure becomes a soft "failed" receipt.

Action shape (the caller — dashboard approve / finalize — supplies it; the
free-text ledger row is only the correlation via ``action_id``):
    {"type": "calendar.create_event", "event":   {title, start, end, attendees?, ...}}
    {"type": "email.send",            "message": {to, subject, body}}
Fields may also be inlined alongside ``type`` instead of nested.
"""
from __future__ import annotations

from . import google_client, ledger
from .config import settings

CALENDAR_CREATE = "calendar.create_event"
EMAIL_SEND = "email.send"
NATIVE_ACTION_TYPES = frozenset({CALENDAR_CREATE, EMAIL_SEND})


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
        else:  # EMAIL_SEND
            result = google_client.send_gmail(org, action.get("message") or action)
            what, receipt = "email", result.get("message_id") or ""
    except Exception as e:  # noqa: BLE001 — google_client already soft-returns; belt+braces
        result = {"ok": False, "error": f"executor error ({type(e).__name__})"}
        what, receipt = atype, ""

    # Write provenance back through the existing status weld point: "done" closes
    # the ledger row and shows a receipt in the dashboard; "failed" surfaces why.
    aid = (action_id or "").strip()
    if aid:
        try:
            if result.get("ok"):
                detail = " · ".join(p for p in ("native", what, receipt) if p)[:300]
                ledger.set_action_status(aid, "done", detail, org_id=org)
            else:
                detail = f"native · {result.get('error', 'failed')}"[:300]
                ledger.set_action_status(aid, "failed", detail, org_id=org)
        except Exception as e:  # noqa: BLE001 — provenance is best-effort
            print(
                f"[executor] status write skipped ({type(e).__name__})", flush=True
            )
    return result
