"""In-memory, resettable state for the Northstar demo product.

Deliberately process-local and network-free: no database, no external service.
`reset()` restores the frozen seed exactly, which is what lets the demo be run
back-to-back and lets every test start from an identical world.

The guarded operation lives here as two explicit phases:
  * `preview_followup_task()` — pure read, returns the EXACT record that would
    be written. Never mutates. This is what a caller shows for approval.
  * `create_followup_task()` — the idempotent write. Keyed on a stable
    idempotency key, so the first approved execution creates the task and every
    later execution of the same decision returns that same task, created=False.

Nothing here knows about Laura's approval system. The product only exposes the
controlled write; the decision to call it lives entirely upstream.
"""
from __future__ import annotations

import threading
from typing import Any

from . import seed

_LOCK = threading.Lock()
_STATE: dict[str, Any] = seed.seed_state()


def reset() -> None:
    """Restore the frozen seed. Idempotent: two resets == one reset."""
    global _STATE
    with _LOCK:
        _STATE = seed.seed_state()


def snapshot() -> dict[str, Any]:
    """A read-only-ish deep-ish view for the pages. Callers must not mutate."""
    with _LOCK:
        return _STATE


# ── read helpers ───────────────────────────────────────────────────────────
def company() -> dict[str, Any]:
    return _STATE["company"]


def customers() -> list[dict[str, Any]]:
    return _STATE["customers"]


def customer(cid: str) -> dict[str, Any] | None:
    return next((c for c in _STATE["customers"] if c["id"] == cid), None)


def onboarding_checklist() -> list[dict[str, Any]]:
    return _STATE["onboarding_checklist"]


def onboarding_stages() -> list[dict[str, Any]]:
    return seed.ONBOARDING_STAGES


def stage_by_slug(slug: str) -> dict[str, Any] | None:
    return next((s for s in seed.ONBOARDING_STAGES if s["slug"] == slug), None)


def implementation() -> dict[str, Any]:
    return _STATE["implementation"]


def tasks() -> list[dict[str, Any]]:
    return _STATE["tasks"]


def security_settings() -> list[dict[str, Any]]:
    return _STATE["security_settings"]


def task_by_idempotency_key(key: str) -> dict[str, Any] | None:
    return next((t for t in _STATE["tasks"] if t.get("idempotency_key") == key), None)


# ── the guarded follow-up operation ────────────────────────────────────────
def _followup_record(idempotency_key: str, title: str, note: str) -> dict[str, Any]:
    """The exact shape of the task the operation would create. Deterministic:
    the id is assigned only at write time, so preview shows id=None."""
    return {
        "title": title,
        "note": note,
        "customer_id": seed.CUSTOMER_ID,
        "assignee": seed.FOLLOWUP_ASSIGNEE,
        "status": "open",
        "idempotency_key": idempotency_key,
        "created_at": seed.DEMO_NOW,
    }


def preview_followup_task(
    idempotency_key: str | None = None,
    title: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """PURE READ. Return the exact task that a subsequent create would write,
    plus whether that write would be a no-op (the decision was already
    executed). Never mutates state."""
    key = (idempotency_key or seed.FOLLOWUP_IDEMPOTENCY_KEY).strip()
    record = _followup_record(
        key,
        (title or seed.FOLLOWUP_TITLE).strip(),
        (note or seed.FOLLOWUP_NOTE).strip(),
    )
    with _LOCK:
        existing = task_by_idempotency_key(key)
    return {
        "would_create": existing is None,
        "idempotency_key": key,
        "task": {**record, "id": existing["id"] if existing else None},
    }


def create_followup_task(
    idempotency_key: str | None = None,
    title: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """The idempotent write. First call with a given key creates the task;
    every later call with the SAME key returns that task with created=False and
    writes nothing. Returns a receipt the caller can surface."""
    key = (idempotency_key or seed.FOLLOWUP_IDEMPOTENCY_KEY).strip()
    with _LOCK:
        existing = task_by_idempotency_key(key)
        if existing is not None:
            return {
                "created": False,
                "task": existing,
                "receipt": {
                    "status": "exists",
                    "task_id": existing["id"],
                    "idempotency_key": key,
                    "created_at": existing["created_at"],
                },
            }
        seq = _STATE["_next_task_seq"]
        _STATE["_next_task_seq"] = seq + 1
        task = {
            "id": f"task-{seq:04d}",
            **_followup_record(
                key,
                (title or seed.FOLLOWUP_TITLE).strip(),
                (note or seed.FOLLOWUP_NOTE).strip(),
            ),
        }
        _STATE["tasks"].append(task)
        return {
            "created": True,
            "task": task,
            "receipt": {
                "status": "created",
                "task_id": task["id"],
                "idempotency_key": key,
                "created_at": task["created_at"],
            },
        }
