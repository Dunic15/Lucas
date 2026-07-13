"""Postgres DAL for the durable callback/action outbox.

Every tenant operation sets transaction-local app.current_org before its first
query, so the exact laura_app runtime role remains FORCE-RLS bound. The one
private function returns due org UUIDs only; payloads are always claimed under
that org's ordinary RLS context.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text

from . import control_plane
from .config import settings


def _engine():
    return control_plane._get_engine()


def _set_org(conn, org_id: str) -> None:
    control_plane._set_org(conn, org_id)


def _callback_insert(conn, callback: dict[str, Any]) -> Optional[int]:
    row = conn.execute(
        text(
            """
            INSERT INTO callback_outbox (
              org_id, idempotency_key, bot_id, action_id, event,
              callback_url, team_id, channel, external_ref_json,
              payload_json, status, attempts, next_attempt_at,
              last_error, created_at, updated_at
            ) VALUES (
              :org_id, :idempotency_key, :bot_id, :action_id, :event,
              :callback_url, :team_id, :channel,
              CAST(:external_ref_json AS jsonb),
              CAST(:payload_json AS jsonb),
              'pending', 0,
              clock_timestamp()
                + (:not_before_seconds * interval '1 second'),
              '', clock_timestamp(), clock_timestamp()
            )
            ON CONFLICT (org_id, idempotency_key) DO NOTHING
            RETURNING id
            """
        ),
        {
            **callback,
            "not_before_seconds": max(
                0.0, min(float(callback.get("not_before_seconds") or 0), 30.0)
            ),
            "external_ref_json": json.dumps(
                callback.get("external_ref") or {},
                separators=(",", ":"),
                sort_keys=True,
            ),
            "payload_json": json.dumps(
                callback.get("payload") or {},
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    ).fetchone()
    if row is None:
        row = conn.execute(
            text(
                "SELECT id FROM callback_outbox "
                "WHERE org_id=:org_id AND idempotency_key=:idempotency_key"
            ),
            callback,
        ).fetchone()
    return int(row[0]) if row else None


def _capture_item(row: Any) -> dict[str, str]:
    return {
        "action_id": str(row["action_id"]),
        "action": str(row["action"]),
        "owner": str(row["owner"]),
        "due": str(row["due"]),
    }


def persist_action_capture_once(
    org_id: str,
    bot_id: str,
    item: dict[str, Any],
    callback: Optional[dict[str, Any]],
    *,
    source_event_key: str = "",
    source_fingerprint: str = "",
    dedupe_window_seconds: float = 30.0,
) -> tuple[dict[str, str], bool, Optional[int]]:
    """Atomically dedupe a Recall final, then persist action + callback.

    Only hashes enter these columns.  Exact source keys (signed webhook id, or
    transcript id + relative word timing) dedupe for the life of the meeting.
    The fingerprint path is a bounded fallback for legacy payloads with no
    timing.  An org-scoped advisory transaction lock makes concurrent workers
    and process restarts converge on one action_id and one callback row.
    """
    event_key = str(source_event_key or "")[:128]
    fingerprint = str(source_fingerprint or "")[:128]
    window = max(1.0, min(float(dedupe_window_seconds), 300.0))
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        lock_key = event_key or fingerprint
        if lock_key:
            conn.execute(
                text(
                    "SELECT pg_catalog.pg_advisory_xact_lock("
                    "pg_catalog.hashtextextended(:lock_key, 0))"
                ),
                {"lock_key": f"{org_id}:{bot_id}:{lock_key}"},
            )

        existing = None
        if event_key:
            existing = conn.execute(
                text(
                    """
                    SELECT action_id, action, owner, due
                    FROM queued_actions
                    WHERE org_id=:org_id AND source_event_key=:source_event_key
                    LIMIT 1
                    """
                ),
                {"org_id": org_id, "source_event_key": event_key},
            ).mappings().first()
        elif fingerprint:
            existing = conn.execute(
                text(
                    """
                    SELECT action_id, action, owner, due
                    FROM queued_actions
                    WHERE org_id=:org_id AND bot_id=:bot_id
                      AND source_fingerprint=:source_fingerprint
                      AND created_at >= clock_timestamp()
                        - (:window * interval '1 second')
                    ORDER BY created_at DESC, action_id
                    LIMIT 1
                    """
                ),
                {
                    "org_id": org_id,
                    "bot_id": bot_id,
                    "source_fingerprint": fingerprint,
                    "window": window,
                },
            ).mappings().first()
        if existing is not None:
            return _capture_item(existing), False, None

        values = {
            "org_id": org_id,
            "bot_id": bot_id,
            "action_id": str(item.get("action_id") or ""),
            "action": str(item.get("action") or "")[:300],
            "owner": str(item.get("owner") or "")[:100],
            "due": str(item.get("due") or "")[:100],
            "source_event_key": event_key,
            "source_fingerprint": fingerprint,
        }
        row = conn.execute(
            text(
                """
                INSERT INTO queued_actions (
                  org_id, bot_id, action_id, action, owner, due,
                  source_event_key, source_fingerprint,
                  created_at, updated_at
                ) VALUES (
                  :org_id, :bot_id, :action_id, :action, :owner, :due,
                  :source_event_key, :source_fingerprint,
                  clock_timestamp(), clock_timestamp()
                )
                RETURNING action_id, action, owner, due
                """
            ),
            values,
        ).mappings().one()
        outbox_id = _callback_insert(conn, callback) if callback else None
        return _capture_item(row), True, outbox_id


def persist_action_capture(
    org_id: str,
    bot_id: str,
    item: dict[str, Any],
    callback: Optional[dict[str, Any]],
) -> Optional[int]:
    """Compatibility wrapper for non-Recall callers without a source key."""
    _item, _created, outbox_id = persist_action_capture_once(
        org_id, bot_id, item, callback
    )
    return outbox_id


def extend_action_capture_once(
    org_id: str,
    bot_id: str,
    action_id: str,
    fragment: str,
    *,
    source_event_key: str = "",
    source_fingerprint: str = "",
    dedupe_window_seconds: float = 30.0,
) -> tuple[dict[str, str], bool]:
    """Append one ASR continuation and refresh its pending callback atomically."""
    event_key = str(source_event_key or "")[:128]
    fingerprint = str(source_fingerprint or "")[:128]
    window = max(1.0, min(float(dedupe_window_seconds), 300.0))
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                "SELECT pg_catalog.pg_advisory_xact_lock("
                "pg_catalog.hashtextextended(:lock_key, 0))"
            ),
            {"lock_key": f"{org_id}:{bot_id}:action:{action_id}"},
        )
        row = conn.execute(
            text(
                """
                SELECT action_id, action, owner, due
                FROM queued_actions
                WHERE org_id=:org_id AND bot_id=:bot_id
                  AND action_id=:action_id
                FOR UPDATE
                """
            ),
            {
                "org_id": org_id,
                "bot_id": bot_id,
                "action_id": action_id,
            },
        ).mappings().first()
        if row is None:
            raise RuntimeError("queued action missing during continuation")

        duplicate = None
        if event_key:
            duplicate = conn.execute(
                text(
                    """
                    SELECT 1 FROM action_capture_events
                    WHERE org_id=:org_id AND action_id=:action_id
                      AND source_event_key=:source_event_key
                    LIMIT 1
                    """
                ),
                {
                    "org_id": org_id,
                    "action_id": action_id,
                    "source_event_key": event_key,
                },
            ).first()
        elif fingerprint:
            duplicate = conn.execute(
                text(
                    """
                    SELECT 1 FROM action_capture_events
                    WHERE org_id=:org_id AND action_id=:action_id
                      AND source_fingerprint=:source_fingerprint
                      AND created_at >= clock_timestamp()
                        - (:window * interval '1 second')
                    LIMIT 1
                    """
                ),
                {
                    "org_id": org_id,
                    "action_id": action_id,
                    "source_fingerprint": fingerprint,
                    "window": window,
                },
            ).first()
        if duplicate is not None:
            return _capture_item(row), False

        updated = " ".join(
            (str(row["action"]) + " " + str(fragment or "")).split()
        )[:300]
        conn.execute(
            text(
                """
                INSERT INTO action_capture_events (
                  org_id, action_id, source_event_key,
                  source_fingerprint, created_at
                ) VALUES (
                  :org_id, :action_id, :source_event_key,
                  :source_fingerprint, clock_timestamp()
                )
                """
            ),
            {
                "org_id": org_id,
                "action_id": action_id,
                "source_event_key": event_key,
                "source_fingerprint": fingerprint,
            },
        )
        conn.execute(
            text(
                """
                UPDATE queued_actions
                SET action=:action, updated_at=clock_timestamp()
                WHERE org_id=:org_id AND bot_id=:bot_id
                  AND action_id=:action_id
                """
            ),
            {
                "org_id": org_id,
                "bot_id": bot_id,
                "action_id": action_id,
                "action": updated,
            },
        )
        # action.requested starts behind a settle fence.  A continuation both
        # changes the durable action and the exact pending wire payload, then
        # nudges the fence so a concurrent worker cannot observe half-state.
        callback = conn.execute(
            text(
                """
                UPDATE callback_outbox
                SET payload_json=jsonb_set(
                      payload_json, '{action}',
                      to_jsonb(CAST(:action AS text)), true
                    ),
                    next_attempt_at=GREATEST(
                      next_attempt_at,
                      clock_timestamp() + interval '1 second'
                    ),
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND action_id=:action_id
                  AND event='action.requested'
                  AND status IN ('pending', 'failed')
                RETURNING id
                """
            ),
            {
                "org_id": org_id,
                "action_id": action_id,
                "action": updated,
            },
        ).first()
        if callback is None:
            status = conn.execute(
                text(
                    """
                    SELECT status FROM callback_outbox
                    WHERE org_id=:org_id AND action_id=:action_id
                      AND event='action.requested'
                    """
                ),
                {"org_id": org_id, "action_id": action_id},
            ).first()
            if status is not None:
                raise RuntimeError("action callback escaped settle fence")
        return {
            "action_id": str(row["action_id"]),
            "action": updated,
            "owner": str(row["owner"]),
            "due": str(row["due"]),
        }, True


def update_queued_action(
    org_id: str, bot_id: str, item: dict[str, Any]
) -> None:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                """
                INSERT INTO queued_actions (
                  org_id, bot_id, action_id, action, owner, due,
                  created_at, updated_at
                ) VALUES (
                  :org_id, :bot_id, :action_id, :action, :owner, :due,
                  clock_timestamp(), clock_timestamp()
                )
                ON CONFLICT (org_id, action_id) DO UPDATE SET
                  action=excluded.action,
                  owner=excluded.owner,
                  due=excluded.due,
                  updated_at=clock_timestamp()
                """
            ),
            {
                "org_id": org_id,
                "bot_id": bot_id,
                "action_id": str(item.get("action_id") or ""),
                "action": str(item.get("action") or "")[:300],
                "owner": str(item.get("owner") or "")[:100],
                "due": str(item.get("due") or "")[:100],
            },
        )


def queued_actions(org_id: str, bot_id: str) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT action_id, action, owner, due
                FROM queued_actions
                WHERE org_id=:org_id AND bot_id=:bot_id
                ORDER BY created_at, action_id
                """
            ),
            {"org_id": org_id, "bot_id": bot_id},
        ).mappings().all()
    return [dict(row) for row in rows]


def enqueue_callback(callback: dict[str, Any]) -> Optional[int]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, callback["org_id"])
        return _callback_insert(conn, callback)


def due_orgs(limit: int = 20) -> list[str]:
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM laura_private.due_callback_orgs(:limit)"),
            {"limit": max(1, min(int(limit), 100))},
        ).fetchall()
    return [str(row[0]) for row in rows]


def claim_due(
    org_id: str,
    limit: int,
    *,
    outbox_id: Optional[int] = None,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    engine = _engine()
    clock = "clock_timestamp()" if now is None else "to_timestamp(:now)"
    # A claim must outlive the configured HTTP timeout, otherwise a slow but
    # healthy Cedric response could still be in flight when another App Runner
    # instance reclaims and sends the same event concurrently.
    lease_seconds = max(
        60, int(float(settings.callback_timeout_seconds)) + 30
    )
    id_filter = "" if outbox_id is None else "AND id=:outbox_id"
    query = text(
        f"""
        WITH due AS (
          SELECT id
          FROM callback_outbox
          WHERE org_id=:org_id
            AND (
              (
                status IN ('pending', 'failed')
                AND next_attempt_at IS NOT NULL
                AND next_attempt_at <= {clock}
              ) OR (
                status='sending'
                AND lease_until IS NOT NULL
                AND lease_until <= {clock}
              )
            )
            {id_filter}
          ORDER BY
            CASE WHEN status='sending' THEN lease_until ELSE next_attempt_at END,
            id
          FOR UPDATE SKIP LOCKED
          LIMIT :limit
        )
        UPDATE callback_outbox AS o
        SET status='sending',
            lease_token=gen_random_uuid(),
            lease_until={clock}
              + (:lease_seconds * interval '1 second'),
            updated_at={clock}
        FROM due
        WHERE o.id=due.id
        RETURNING
          o.id, o.org_id, o.idempotency_key, o.bot_id, o.action_id,
          o.event, o.callback_url, o.team_id, o.channel,
          o.external_ref_json, o.payload_json, o.status, o.attempts,
          o.lease_token
        """
    )
    params: dict[str, Any] = {
        "org_id": org_id,
        "limit": max(1, min(int(limit), 100)),
        "lease_seconds": lease_seconds,
    }
    if outbox_id is not None:
        params["outbox_id"] = int(outbox_id)
    if now is not None:
        params["now"] = float(now)
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(query, params).mappings().all()
    return [dict(row) for row in rows]


def finish_attempt(
    org_id: str,
    outbox_id: int,
    lease_token: Any,
    *,
    delivered: bool,
    next_attempt_at: Optional[float],
    last_error: str,
) -> bool:
    engine = _engine()
    if delivered:
        assignments = """
          status='delivered', attempts=attempts+1,
          next_attempt_at=NULL, last_error='',
          delivered_at=clock_timestamp(),
          lease_token=NULL, lease_until=NULL,
          updated_at=clock_timestamp()
        """
        params: dict[str, Any] = {}
    else:
        assignments = """
          status='failed', attempts=attempts+1,
          next_attempt_at=CASE
            WHEN :next_attempt_at IS NULL THEN NULL
            ELSE to_timestamp(:next_attempt_at)
          END,
          last_error=:last_error,
          lease_token=NULL, lease_until=NULL,
          updated_at=clock_timestamp()
        """
        params = {
            "next_attempt_at": next_attempt_at,
            "last_error": (last_error or "")[:160],
        }
    params.update(
        {
            "org_id": org_id,
            "outbox_id": int(outbox_id),
            "lease_token": str(lease_token),
        }
    )
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                f"""
                UPDATE callback_outbox
                SET {assignments}
                WHERE org_id=:org_id AND id=:outbox_id
                  AND status='sending' AND lease_token=:lease_token
                """
            ),
            params,
        )
    return bool(result.rowcount)


def delivery_rows(org_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT id, event, bot_id, action_id, team_id, channel,
                       status, attempts,
                       COALESCE(extract(epoch from next_attempt_at), 0)
                         AS next_attempt_at,
                       last_error,
                       extract(epoch from created_at) AS created_at,
                       extract(epoch from delivered_at) AS delivered_at
                FROM callback_outbox
                WHERE org_id=:org_id
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            {"org_id": org_id, "limit": max(1, min(int(limit), 500))},
        ).mappings().all()
    return [dict(row) for row in rows]


def retry(org_id: str, outbox_id: int) -> str:
    """Return queued | already_delivered | busy | missing."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT status,
                       lease_until IS NOT NULL
                         AND lease_until > clock_timestamp() AS leased
                FROM callback_outbox
                WHERE org_id=:org_id AND id=:outbox_id
                FOR UPDATE
                """
            ),
            {"org_id": org_id, "outbox_id": int(outbox_id)},
        ).fetchone()
        if row is None:
            return "missing"
        if row[0] == "delivered":
            return "already_delivered"
        if row[0] == "sending" and bool(row[1]):
            return "busy"
        conn.execute(
            text(
                """
                UPDATE callback_outbox
                SET status='pending', next_attempt_at=clock_timestamp(),
                    last_error='', lease_token=NULL, lease_until=NULL,
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=:outbox_id
                """
            ),
            {"org_id": org_id, "outbox_id": int(outbox_id)},
        )
    return "queued"
