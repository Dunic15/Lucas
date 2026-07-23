"""Best-effort, metadata-only security audit events.

The audit path is deliberately decoupled from user requests and from the live
meeting loop. Callers only enqueue a small immutable event into a bounded
in-memory queue; a daemon worker performs the tenant-scoped Postgres INSERT.
When the control plane is disabled, an identifier is unsafe, the queue is full,
or Postgres is unavailable, the event is dropped before it can add latency or
break the product.

Only opaque identifiers are accepted. Titles, transcript/document text,
emails, request bodies, credentials, and receipts cannot pass the identifier
validator and therefore cannot enter the audit table accidentally.
"""
from __future__ import annotations

from dataclasses import dataclass
import queue
import re
import threading
import uuid

from . import control_plane

_MAX_QUEUE = 1024
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,119}$")
_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")

_QUEUE: queue.Queue["AuditEvent"] = queue.Queue(maxsize=_MAX_QUEUE)
_WORKER_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    org_id: str
    actor_user_id: str | None
    action: str
    target: str | None


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _event(
    org_id: str,
    actor_user_id: str | None,
    action: str,
    target: str | None,
) -> AuditEvent | None:
    org = _uuid(org_id)
    safe_action = str(action or "").strip().lower()
    safe_target = str(target or "").strip() or None
    if org is None or _ACTION_RE.fullmatch(safe_action) is None:
        return None
    if safe_target is not None and _TARGET_RE.fullmatch(safe_target) is None:
        return None
    # Machine/service actions have no human actor. A malformed human id is
    # treated as unknown rather than being persisted in the UUID column.
    actor = _uuid(actor_user_id or "")
    return AuditEvent(org, actor, safe_action, safe_target)


def _write(event: AuditEvent) -> None:
    """One RLS-scoped INSERT. Exceptions are handled only by the worker."""
    from sqlalchemy import text

    engine = control_plane._get_engine()
    if engine is None:
        return
    with engine.begin() as conn:
        control_plane._set_org(conn, event.org_id)
        conn.execute(
            text(
                "INSERT INTO audit_log "
                "(org_id, actor_user_id, action, target) "
                "VALUES (:org, CAST(:actor AS uuid), :action, :target)"
            ),
            {
                "org": event.org_id,
                "actor": event.actor_user_id,
                "action": event.action,
                "target": event.target,
            },
        )


def _drain() -> None:
    while True:
        event = _QUEUE.get()
        try:
            _write(event)
        except Exception as exc:  # noqa: BLE001 - audit must never break callers
            # Error class only: never render event fields, identities, or DSNs.
            print(
                f"[audit] event dropped ({type(exc).__name__})",
                flush=True,
            )
        finally:
            _QUEUE.task_done()


def _ensure_worker() -> None:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(
            target=_drain,
            name="laura-security-audit",
            daemon=True,
        )
        _WORKER.start()


def record(
    org_id: str,
    *,
    actor_user_id: str | None,
    action: str,
    target: str | None = None,
) -> bool:
    """Enqueue an audit event without blocking.

    False means the event was intentionally dropped (disabled control plane,
    unsafe metadata, or backpressure). Callers must never retry inline:
    availability and live-meeting latency win over audit completeness.
    """
    if not control_plane.enabled():
        return False
    event = _event(org_id, actor_user_id, action, target)
    if event is None:
        return False
    try:
        _QUEUE.put_nowait(event)
    except queue.Full:
        return False
    _ensure_worker()
    return True


def queue_depth() -> int:
    """Operational signal for health/metrics; contains no event data."""
    return _QUEUE.qsize()
