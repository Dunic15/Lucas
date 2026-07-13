"""Durable, org-scoped callback outbox and queued-action store.

Production uses the FORCE-RLS Postgres control plane: capture/enqueue commits in
one bounded tenant transaction, and multi-instance workers claim with SKIP
LOCKED plus expiring leases. The key-free demo retains the local SQLite path.
The idempotency key is stable across retries; Cedric must dedupe it because a
crash can happen after its 2xx and before Laura commits delivered_at.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from . import control_plane, outbox_pg, store
from .config import settings

_RETRY_SECONDS = (1.0, 5.0, 30.0, 120.0, 600.0)
class OutboxUnavailable(RuntimeError):
    """Configured durable store is unavailable; callers must fail closed."""


def _pg_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except OutboxUnavailable:
        raise
    except Exception as exc:
        raise OutboxUnavailable(type(exc).__name__) from exc


_ROUTING_KEYS = {
    "team", "team_id", "slack_channel", "channel", "thread_ts",
    "booking_id", "meeting_id",
}

# Defense in depth: enqueue_session_ended is callable outside cedric.integration,
# so the storage boundary has its own allowlist. Raw transcript/utterance fields
# and meeting URLs can never enter callback_outbox even if a future caller passes
# the complete local archive object directly.
_ARTIFACT_KEYS = {
    "summary", "decisions", "actions", "checklist", "missing_steps",
    "readiness_score", "risks", "follow_up_email", "avatar_id", "org_id",
    "duration_seconds", "artifact_version",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema() -> None:
    if control_plane.enabled():
        return
    with store._LOCK, store._connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS queued_actions (
                org_id TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                action TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT '',
                due TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (org_id, action_id)
            );
            CREATE INDEX IF NOT EXISTS idx_queued_actions_bot
                ON queued_actions(org_id, bot_id, created_at);

            CREATE TABLE IF NOT EXISTS callback_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                org_id TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                action_id TEXT NOT NULL DEFAULT '',
                event TEXT NOT NULL,
                callback_url TEXT NOT NULL,
                team_id TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT '',
                external_ref_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                delivered_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_callback_outbox_due
                ON callback_outbox(status, next_attempt_at);
            CREATE INDEX IF NOT EXISTS idx_callback_outbox_org
                ON callback_outbox(org_id, created_at DESC);
            """
        )


def _safe_external_ref(integration: dict | None) -> dict[str, str]:
    ref = (integration or {}).get("external_ref") or {}
    if not isinstance(ref, dict):
        return {}
    return {
        key: str(value)[:200]
        for key, value in ref.items()
        if key in _ROUTING_KEYS and isinstance(value, (str, int, float))
    }


def _routing(integration: dict | None) -> tuple[str, str, str, dict[str, str]]:
    integration = integration or {}
    org_id = str(integration.get("org_id") or settings.demo_org_id)
    ref = _safe_external_ref(integration)
    team = str(
        ref.get("team")
        or ref.get("team_id")
        or integration.get("team_id")
        or ""
    )[:100]
    channel = str(
        ref.get("slack_channel")
        or ref.get("channel")
        or integration.get("channel")
        or ""
    )[:100]
    return org_id, team, channel, ref


def persist_queued_action(session: Any, item: dict) -> None:
    """Update one stable action. Production writes only the durable PG row."""
    org_id = str(getattr(session, "org_id", "") or settings.demo_org_id)
    if control_plane.enabled():
        _pg_call(outbox_pg.update_queued_action, org_id, str(session.bot_id), item)
        return
    _ensure_schema()
    now = time.time()
    with store._LOCK, store._connect() as conn:
        conn.execute(
            """
            INSERT INTO queued_actions
                (org_id, bot_id, action_id, action, owner, due, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(org_id, action_id) DO UPDATE SET
                action=excluded.action, owner=excluded.owner,
                due=excluded.due, updated_at=excluded.updated_at
            """,
            (
                org_id, session.bot_id, str(item.get("action_id") or ""),
                str(item.get("action") or "")[:300],
                str(item.get("owner") or "")[:100],
                str(item.get("due") or "")[:100], now, now,
            ),
        )


def persist_action_capture(session: Any, item: dict) -> int | None:
    """Persist action + callback atomically before any spoken confirmation."""
    integration = dict(getattr(session, "integration", None) or {})
    org_id, team, channel, ref = _routing(
        {**integration, "org_id": getattr(session, "org_id", "")}
    )
    action_id = str((item or {}).get("action_id") or "").strip()
    callback_record = None
    if action_id and str(integration.get("callback_url") or "").strip():
        payload = {
            "event": "action.requested", "bot_id": str(session.bot_id),
            "action_id": action_id, "org_id": org_id, "external_ref": ref,
            "action": str((item or {}).get("action") or "")[:300],
            "owner": str((item or {}).get("owner") or "")[:100],
            "due": str((item or {}).get("due") or "")[:100],
            "at": _now_iso(),
        }
        callback_record = {
            "org_id": org_id,
            "idempotency_key": f"action.requested:{action_id}",
            "bot_id": str(session.bot_id), "action_id": action_id,
            "event": "action.requested",
            "callback_url": str(integration.get("callback_url") or "").strip(),
            "team_id": team, "channel": channel,
            "external_ref": ref, "payload": payload,
        }
    if control_plane.enabled():
        return _pg_call(
            outbox_pg.persist_action_capture, org_id,
            str(session.bot_id), item, callback_record,
        )
    persist_queued_action(session, item)
    if callback_record:
        return _enqueue(
            event="action.requested",
            idempotency_key=callback_record["idempotency_key"],
            integration={**integration, "org_id": org_id},
            bot_id=str(session.bot_id), action_id=action_id,
            payload=callback_record["payload"],
        )
    return None


def queued_actions(org_id: str, bot_id: str) -> list[dict]:
    if control_plane.enabled():
        return _pg_call(
            outbox_pg.queued_actions, org_id or settings.demo_org_id, bot_id
        )
    _ensure_schema()
    with store._LOCK, store._connect() as conn:
        rows = conn.execute(
            """
            SELECT action_id, action, owner, due
            FROM queued_actions
            WHERE org_id=? AND bot_id=?
            ORDER BY created_at, action_id
            """,
            (org_id or settings.demo_org_id, bot_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _enqueue(
    *, event: str, idempotency_key: str, integration: dict,
    bot_id: str, action_id: str, payload: dict,
) -> int | None:
    callback_url = str((integration or {}).get("callback_url") or "").strip()
    if not callback_url:
        return None
    org_id, team, channel, ref = _routing(integration)
    if control_plane.enabled():
        return _pg_call(
            outbox_pg.enqueue_callback,
            {
                "org_id": org_id, "idempotency_key": idempotency_key,
                "bot_id": bot_id, "action_id": action_id, "event": event,
                "callback_url": callback_url, "team_id": team,
                "channel": channel, "external_ref": ref, "payload": payload,
            },
        )
    _ensure_schema()
    now = time.time()
    with store._LOCK, store._connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO callback_outbox (
                idempotency_key, org_id, bot_id, action_id, event,
                callback_url, team_id, channel, external_ref_json,
                payload_json, status, attempts, next_attempt_at,
                last_error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, '', ?)
            """,
            (
                idempotency_key, org_id, bot_id, action_id, event,
                callback_url, team, channel,
                json.dumps(ref, separators=(",", ":"), sort_keys=True),
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                now, now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM callback_outbox WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
    return int(row["id"]) if row else None


def enqueue_action_requested(integration: dict, bot_id: str, item: dict) -> int | None:
    org_id, _team, _channel, ref = _routing(integration)
    action_id = str((item or {}).get("action_id") or "").strip()
    if not action_id:
        return None
    payload = {
        "event": "action.requested",
        "bot_id": bot_id,
        "action_id": action_id,
        "org_id": org_id,
        "external_ref": ref,
        "action": str((item or {}).get("action") or "")[:300],
        "owner": str((item or {}).get("owner") or "")[:100],
        "due": str((item or {}).get("due") or "")[:100],
        "at": _now_iso(),
    }
    return _enqueue(
        event="action.requested",
        idempotency_key=f"action.requested:{action_id}",
        integration=integration,
        bot_id=bot_id,
        action_id=action_id,
        payload=payload,
    )


def enqueue_session_ended(
    integration: dict, bot_id: str, artifact: dict
) -> int | None:
    org_id, _team, _channel, ref = _routing(integration)
    payload = {
        "event": "session.ended",
        "bot_id": bot_id,
        "org_id": org_id,
        "external_ref": ref,
        "ended_at": _now_iso(),
        "artifact": {
            key: (artifact or {})[key]
            for key in _ARTIFACT_KEYS
            if key in (artifact or {})
        },
    }
    return _enqueue(
        event="session.ended",
        idempotency_key=f"session.ended:{bot_id}",
        integration=integration,
        bot_id=bot_id,
        action_id="",
        payload=payload,
    )


def reconcile_sessions() -> int:
    """Heal a crash between durable action capture and outbox enqueue."""
    count = 0
    for session in store.all_sessions():
        if not session.integration or not session.integration.get("callback_url"):
            continue
        for item in queued_actions(session.org_id, session.bot_id):
            if enqueue_action_requested(
                dict(session.integration), session.bot_id, item
            ) is not None:
                count += 1
    return count


def _claim_due(
    now: float, limit: int, *, org_id: str | None = None,
    outbox_id: int | None = None,
) -> list[dict]:
    if control_plane.enabled():
        if org_id is not None:
            return _pg_call(
                outbox_pg.claim_due, org_id or settings.demo_org_id, limit,
                outbox_id=outbox_id, now=now,
            )
        claimed: list[dict] = []
        for due_org in _pg_call(outbox_pg.due_orgs, limit):
            remaining = limit - len(claimed)
            if remaining <= 0:
                break
            claimed.extend(
                _pg_call(outbox_pg.claim_due, due_org, remaining, now=now)
            )
        return claimed
    _ensure_schema()
    filters = [
        """(
            ((status='pending' OR (status='failed' AND next_attempt_at > 0))
             AND next_attempt_at <= ?)
            OR (status='sending' AND next_attempt_at <= ?)
        )"""
    ]
    params: list[Any] = [now, now]
    if org_id is not None:
        filters.append("org_id=?")
        params.append(org_id or settings.demo_org_id)
    if outbox_id is not None:
        filters.append("id=?")
        params.append(int(outbox_id))
    params.append(int(limit))
    query = (
        "SELECT * FROM callback_outbox WHERE " + " AND ".join(filters)
        + " ORDER BY next_attempt_at, id LIMIT ?"
    )
    with store._LOCK, store._connect() as conn:
        rows = conn.execute(query, params).fetchall()
        claimed: list[dict] = []
        for row in rows:
            cur = conn.execute(
                """
                UPDATE callback_outbox
                SET status='sending', next_attempt_at=?
                WHERE id=? AND (
                    ((status='pending' OR (status='failed' AND next_attempt_at > 0))
                     AND next_attempt_at <= ?)
                    OR (status='sending' AND next_attempt_at <= ?)
                )
                """,
                (now + 60.0, row["id"], now, now),
            )
            if cur.rowcount:
                claimed.append(dict(row))
    return claimed


def process_due(
    *, limit: int = 20, now: float | None = None,
    org_id: str | None = None, outbox_id: int | None = None,
) -> int:
    """Deliver due rows without sleeping; claims are safe across instances."""
    from .cedric import callback
    current = time.time() if now is None else float(now)
    delivered_count = 0
    for row in _claim_due(current, limit, org_id=org_id, outbox_id=outbox_id):
        try:
            raw_payload = row["payload_json"]
            payload = (
                json.loads(raw_payload)
                if isinstance(raw_payload, str) else dict(raw_payload)
            )
            response = callback._post(
                row["callback_url"], payload,
                idempotency_key=row["idempotency_key"],
            )
            ok = 200 <= response.status_code < 300
            reason = "" if ok else f"HTTP {response.status_code}"
            transient = (
                response.status_code in {408, 425, 429}
                or response.status_code >= 500
            )
        except Exception as exc:
            ok, transient, reason = False, True, type(exc).__name__
        attempts = int(row["attempts"]) + 1
        if control_plane.enabled():
            if ok:
                if _pg_call(
                    outbox_pg.finish_attempt, str(row["org_id"]),
                    int(row["id"]), row["lease_token"], delivered=True,
                    next_attempt_at=None, last_error="",
                ):
                    delivered_count += 1
                continue
            delay = _RETRY_SECONDS[min(attempts - 1, len(_RETRY_SECONDS) - 1)]
            _pg_call(
                outbox_pg.finish_attempt, str(row["org_id"]),
                int(row["id"]), row["lease_token"], delivered=False,
                next_attempt_at=(current + delay if transient else None),
                last_error=reason,
            )
            continue
        if ok:
            with store._LOCK, store._connect() as conn:
                conn.execute(
                    """
                    UPDATE callback_outbox
                    SET status='delivered', attempts=?, next_attempt_at=0,
                        last_error='', delivered_at=?
                    WHERE id=?
                    """,
                    (attempts, time.time(), row["id"]),
                )
            delivered_count += 1
            continue
        delay = _RETRY_SECONDS[min(attempts - 1, len(_RETRY_SECONDS) - 1)]
        with store._LOCK, store._connect() as conn:
            conn.execute(
                """
                UPDATE callback_outbox
                SET status='failed', attempts=?, next_attempt_at=?, last_error=?
                WHERE id=?
                """,
                (attempts, current + delay if transient else 0.0,
                 reason[:160], row["id"]),
            )
    return delivered_count


def delivery_rows(org_id: str, *, limit: int = 100) -> list[dict]:
    """PII-safe delivery state; payload and callback URL stay private."""
    if control_plane.enabled():
        return _pg_call(
            outbox_pg.delivery_rows, org_id or settings.demo_org_id, limit=limit
        )
    _ensure_schema()
    with store._LOCK, store._connect() as conn:
        rows = conn.execute(
            """
            SELECT id, event, bot_id, action_id, team_id, channel,
                   status, attempts, next_attempt_at, last_error,
                   created_at, delivered_at
            FROM callback_outbox
            WHERE org_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (org_id or settings.demo_org_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def retry_status(org_id: str, outbox_id: int) -> str:
    if control_plane.enabled():
        return _pg_call(
            outbox_pg.retry, org_id or settings.demo_org_id, int(outbox_id)
        )
    _ensure_schema()
    now = time.time()
    with store._LOCK, store._connect() as conn:
        row = conn.execute(
            """
            SELECT status, next_attempt_at
            FROM callback_outbox WHERE id=? AND org_id=?
            """,
            (int(outbox_id), org_id or settings.demo_org_id),
        ).fetchone()
        if row is None:
            return "missing"
        if row["status"] == "delivered":
            return "already_delivered"
        if (
            row["status"] == "sending"
            and float(row["next_attempt_at"] or 0) > now
        ):
            return "busy"
        conn.execute(
            """
            UPDATE callback_outbox
            SET status='pending', next_attempt_at=?, last_error=''
            WHERE id=? AND org_id=?
            """,
            (now, int(outbox_id), org_id or settings.demo_org_id),
        )
    return "queued"


def retry(org_id: str, outbox_id: int) -> bool:
    """Compatibility bool; dashboard uses retry_status for busy vs missing."""
    return retry_status(org_id, outbox_id) in {
        "queued", "already_delivered"
    }
