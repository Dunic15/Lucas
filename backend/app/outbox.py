"""Durable, org-scoped callback outbox and queued-action store.

SQLite is intentionally local: enqueue is one bounded transaction and delivery
runs outside live speech/finalize. The idempotency key is stable across retries
and process restarts; Cedric must dedupe that key because a crash can happen
after its 2xx and before Laura commits delivered_at.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from . import store
from .config import settings

_RETRY_SECONDS = (1.0, 5.0, 30.0, 120.0, 600.0)
_ROUTING_KEYS = {
    "team", "team_id", "slack_channel", "channel", "thread_ts",
    "booking_id", "meeting_id",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema() -> None:
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
    """Persist one stable action_id before acknowledging capture."""
    _ensure_schema()
    org_id = str(getattr(session, "org_id", "") or settings.demo_org_id)
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
                org_id,
                session.bot_id,
                str(item.get("action_id") or ""),
                str(item.get("action") or "")[:300],
                str(item.get("owner") or "")[:100],
                str(item.get("due") or "")[:100],
                now,
                now,
            ),
        )


def queued_actions(org_id: str, bot_id: str) -> list[dict]:
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
    *,
    event: str,
    idempotency_key: str,
    integration: dict,
    bot_id: str,
    action_id: str,
    payload: dict,
) -> int | None:
    callback_url = str((integration or {}).get("callback_url") or "").strip()
    if not callback_url:
        return None
    _ensure_schema()
    org_id, team, channel, ref = _routing(integration)
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
                idempotency_key,
                org_id,
                bot_id,
                action_id,
                event,
                callback_url,
                team,
                channel,
                json.dumps(ref, separators=(",", ":"), sort_keys=True),
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                now,
                now,
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
            key: value for key, value in (artifact or {}).items()
            if key != "transcript"
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


def _claim_due(now: float, limit: int) -> list[dict]:
    _ensure_schema()
    with store._LOCK, store._connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM callback_outbox
            WHERE (
                (status='pending' OR (status='failed' AND next_attempt_at > 0))
                AND next_attempt_at <= ?
            ) OR (
                status='sending' AND next_attempt_at <= ?
            )
            ORDER BY next_attempt_at, id
            LIMIT ?
            """,
            (now, now, limit),
        ).fetchall()
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


def process_due(*, limit: int = 20, now: float | None = None) -> int:
    """Deliver due rows without sleeping. Safe for a periodic background worker."""
    from .cedric import callback

    current = time.time() if now is None else float(now)
    delivered = 0
    for row in _claim_due(current, limit):
        try:
            payload = json.loads(row["payload_json"])
            response = callback._post(
                row["callback_url"],
                payload,
                idempotency_key=row["idempotency_key"],
            )
            ok = 200 <= response.status_code < 300
            reason = "" if ok else f"HTTP {response.status_code}"
            transient = response.status_code in {408, 425, 429} or response.status_code >= 500
        except Exception as exc:  # transport failures are retryable
            ok = False
            transient = True
            reason = type(exc).__name__

        attempts = int(row["attempts"]) + 1
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
            delivered += 1
            continue

        delay = _RETRY_SECONDS[min(attempts - 1, len(_RETRY_SECONDS) - 1)]
        next_attempt = current + delay if transient else 0.0
        with store._LOCK, store._connect() as conn:
            conn.execute(
                """
                UPDATE callback_outbox
                SET status='failed', attempts=?, next_attempt_at=?,
                    last_error=?
                WHERE id=?
                """,
                (attempts, next_attempt, reason[:160], row["id"]),
            )
    return delivered


def delivery_rows(org_id: str, *, limit: int = 100) -> list[dict]:
    """PII-safe delivery state for dashboard; payload/callback URL stay private."""
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


def retry(org_id: str, outbox_id: int) -> bool:
    """Manual retry is idempotent: delivered rows are never sent again."""
    _ensure_schema()
    with store._LOCK, store._connect() as conn:
        cur = conn.execute(
            """
            UPDATE callback_outbox
            SET status='pending', next_attempt_at=?, last_error=''
            WHERE id=? AND org_id=? AND status!='delivered'
            """,
            (time.time(), int(outbox_id), org_id or settings.demo_org_id),
        )
        if cur.rowcount:
            return True
        row = conn.execute(
            "SELECT status FROM callback_outbox WHERE id=? AND org_id=?",
            (int(outbox_id), org_id or settings.demo_org_id),
        ).fetchone()
    return bool(row and row["status"] == "delivered")
