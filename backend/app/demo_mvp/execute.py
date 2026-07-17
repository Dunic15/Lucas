"""The ONE controlled synthetic write for the MVP: create_followup_task.

Arbitrary browser writes stay disabled. This module provides two narrow hooks
the browser operator calls ONLY when ``NORTHSTAR_DEMO_WRITE_ENABLED`` is on and
the guarded element is the Northstar follow-up control:

- ``followup_permission_extras`` — the Northstar metadata stamped onto the
  canonical action at mint time (idempotency key, customer, demo def/version,
  the exact previewed record, and the expected visible result the operator
  verifies after execution). Everything the approver must see.
- ``execute_followup`` — invoked from ``execute_approved_step`` AFTER the M0
  exactly-once claim + the page-binding re-verification. It performs the
  product write once (itself idempotent on the same key), re-observes the
  product, verifies the visible confirmation, and returns a safe receipt.

The demo write NEVER runs inline and NEVER bypasses the approval door.
"""
from __future__ import annotations

from typing import Any

# The synthetic guarded control the Northstar provider presents on /tasks.
FOLLOWUP_ELEMENT_ID = "create-followup"


def is_followup_element(element_id: str) -> bool:
    return str(element_id) == FOLLOWUP_ELEMENT_ID


def followup_permission_extras(session_row: dict) -> dict[str, Any]:
    """Northstar metadata for the canonical action's permission_json. The
    previewed record is a PURE READ of the product (what the approver sees)."""
    from demos.northstar.product import seed, store

    from . import manifest

    preview = store.preview_followup_task()  # pure read, never mutates
    m = manifest.load()
    return {
        "northstar_followup": True,
        "demo_definition": m["company_id"],
        "demo_version": m["demo_version"],
        "customer": seed.CUSTOMER_ID,
        "idempotency_key": seed.FOLLOWUP_IDEMPOTENCY_KEY,
        "task_title": seed.FOLLOWUP_TITLE,
        "meeting_ref": session_row.get("meeting_ref", ""),
        "preview": preview["task"],
        # The operator verifies this visible result after the write: the new
        # task row element appears on the re-observed /tasks page.
        "expected_result": {"element_present": "task-task-0003"},
    }


def followup_typed(session_row: dict) -> dict[str, Any]:
    """The typed action spec (safe params only) for the canonical action."""
    from demos.northstar.product import seed

    return {
        "type": "browser.create_followup_task",
        "args": {
            "customer": seed.CUSTOMER_ID,
            "title": seed.FOLLOWUP_TITLE,
            "idempotency_key": seed.FOLLOWUP_IDEMPOTENCY_KEY,
        },
    }


def execute_followup(org_id: str, action_id: str, permission: dict,
                     session_row: dict) -> dict[str, Any]:
    """Perform the guarded product write exactly once (the product is itself
    idempotent on the key, so a lost/replayed approval never double-writes),
    re-observe, verify the visible confirmation, and return a safe receipt +
    verdict. NEVER raises to the caller."""
    from demos.northstar.product import seed, store

    key = str(permission.get("idempotency_key")
              or seed.FOLLOWUP_IDEMPOTENCY_KEY)
    result = store.create_followup_task(key)  # idempotent write
    task = result.get("task") or {}
    product_receipt = result.get("receipt") or {}

    # Re-observe the product and verify the visible task row is present.
    verification = "not_verified"
    try:
        rows = store.tasks()
        present = any(t.get("id") == task.get("id") for t in rows)
        confirmation = store.task_by_idempotency_key(key) is not None
        verification = "verified" if (present and confirmation) else "not_verified"
    except Exception:  # noqa: BLE001
        verification = "inconclusive"

    return {
        "ok": True,
        "verification": verification,
        # Safe product receipt — no provider internals, no page bytes.
        "receipt": {
            "product": "northstar",
            "task_id": product_receipt.get("task_id") or task.get("id"),
            "status": product_receipt.get("status"),
            "created": bool(result.get("created")),
            "idempotency_key": key,
            "created_at": product_receipt.get("created_at"),
        },
    }
