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

from . import action_plane
from .. import control_plane
from ..config import settings


_EXECUTION_STATUSES = action_plane.ACTION_STATUSES
_TERMINAL_EXECUTION_STATUSES = action_plane.TERMINAL_STATUSES
# Monotonic repaint guard for at-least-once, possibly out-of-order webhook
# delivery. needs_details and proposed share a rank on purpose: finalize may
# flag an already-proposed card as incomplete, and an edit moves it back —
# both directions are legitimate until a decision lands.
_EXECUTION_RANK = {"": -1, "needs_details": 0, "proposed": 0, "approved": 1,
                   "executing": 2}
_EXECUTION_RANK.update({state: 3 for state in _TERMINAL_EXECUTION_STATUSES})

# Execution claims outlive the slowest vendor call (Google/Asana clients use
# ~30s HTTP timeouts); a lease that expires with the row still 'executing'
# means the process died mid-call — reconciled, never blindly retried.
_EXECUTION_LEASE_SECONDS = 180


class ActionCaptureClosed(RuntimeError):
    """The durable finalizer fenced this bot before a late capture."""


def _engine():
    return control_plane._get_engine()


def _set_org(conn, org_id: str) -> None:
    control_plane._set_org(conn, org_id)


def _capture_finalize_lock(conn, org_id: str, bot_id: str) -> None:
    conn.execute(
        text(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended(:lock_key, 0))"
        ),
        {"lock_key": f"{org_id}:{bot_id}:capture-finalize"},
    )


def _require_capture_open(conn, org_id: str, bot_id: str) -> None:
    row = conn.execute(
        text(
            "SELECT state FROM action_finalize_state "
            "WHERE org_id=:org_id AND bot_id=:bot_id"
        ),
        {"org_id": org_id, "bot_id": bot_id},
    ).first()
    if row is not None:
        raise ActionCaptureClosed(str(row[0]))


# Shared upsert for indexing a session.ended artifact's stable actions into
# queued_actions. Live captures keep the id assigned at capture time; a
# summarizer-only action carries a fresh id. ON CONFLICT(org_id, action_id)
# refreshes owner/due where the live capture had none — identical SQL for the
# row-anchored path (_index_session_ended_actions) and the callback-free path
# (index_session_ended_actions) so the two can never drift.
_INDEX_ACTION_SQL = text(
    """
    INSERT INTO queued_actions (
      org_id, bot_id, action_id, action, owner, due,
      typed_json, params_schema_json, risk, execution_route, origin_avatar,
      execution_status, created_at, updated_at
    ) VALUES (
      :org_id, :bot_id, :action_id, :action, :owner, :due,
      CAST(:typed_json AS jsonb), CAST(:params_schema_json AS jsonb),
      :risk, :execution_route, :origin_avatar,
      :execution_status, clock_timestamp(), clock_timestamp()
    )
    ON CONFLICT (org_id, action_id) DO UPDATE SET
      action=CASE WHEN excluded.action <> ''
                  THEN excluded.action ELSE queued_actions.action END,
      owner=CASE WHEN excluded.owner <> ''
                 THEN excluded.owner ELSE queued_actions.owner END,
      due=CASE WHEN excluded.due <> ''
               THEN excluded.due ELSE queued_actions.due END,
      -- canonical fields fill blanks only (a live-capture row gains its typed
      -- spec at finalize; an already-stamped row is never re-evaluated —
      -- contract re-route bounds)
      typed_json=COALESCE(queued_actions.typed_json, excluded.typed_json),
      params_schema_json=CASE
        WHEN queued_actions.params_schema_json = '[]'::jsonb
        THEN excluded.params_schema_json
        ELSE queued_actions.params_schema_json END,
      risk=CASE WHEN queued_actions.risk = ''
                THEN excluded.risk ELSE queued_actions.risk END,
      execution_route=CASE
        WHEN queued_actions.execution_route = ''
        THEN excluded.execution_route
        ELSE queued_actions.execution_route END,
      origin_avatar=CASE
        WHEN queued_actions.origin_avatar = ''
        THEN excluded.origin_avatar
        ELSE queued_actions.origin_avatar END,
      execution_status=CASE
        WHEN queued_actions.execution_status = ''
        THEN excluded.execution_status
        ELSE queued_actions.execution_status END,
      updated_at=clock_timestamp()
    """
)


def _norm_action_text(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def _indexed_action_params(
    org_id: str, bot_id: str, action: dict, origin_avatar: str = ""
) -> dict[str, Any]:
    typed = action.get("typed") if isinstance(action.get("typed"), dict) else None
    # needs_details is stamped only when a typed spec EXISTS but lacks required
    # parameters (the 'approved but nothing executed' silent no-op). Untyped
    # free-text actions keep '' and the Cedric card path, exactly as today.
    status = ""
    if typed and action_plane.missing_params(typed):
        status = "needs_details"
    return {
        "org_id": org_id,
        "bot_id": bot_id,
        "action_id": str(action.get("action_id") or "").strip()[:128],
        "action": str(action.get("item") or action.get("action") or "")[:300],
        "owner": str(action.get("owner") or "")[:100],
        "due": str(action.get("deadline") or action.get("due") or "")[:100],
        "typed_json": json.dumps(typed, separators=(",", ":"), sort_keys=True)
        if typed else None,
        "params_schema_json": json.dumps(
            action_plane.params_schema(typed), separators=(",", ":")
        ),
        "risk": action_plane.risk_for(typed),
        "execution_route": str(action.get("execution_route") or "")[:16],
        "origin_avatar": str(origin_avatar or "")[:64],
        "execution_status": status,
    }


def _artifact_actions(artifact: Any) -> list[dict]:
    actions = artifact.get("actions") if isinstance(artifact, dict) else []
    return [
        a
        for a in (actions or [])
        if isinstance(a, dict) and str(a.get("action_id") or "").strip()
    ]


def _index_session_ended_actions(
    conn, org_id: str, bot_id: str, outbox_id: int
) -> None:
    """Index the canonical ended artifact's stable actions in this transaction.

    The callback row is the first-write-wins session checkpoint. Reading its
    stored payload (rather than a retry payload) prevents nondeterministic
    summarizer ids from creating phantom actions after a crash.
    """
    row = conn.execute(
        text(
            """
            SELECT payload_json
            FROM callback_outbox
            WHERE org_id=:org_id AND id=:outbox_id
              AND event='session.ended'
            """
        ),
        {"org_id": org_id, "outbox_id": int(outbox_id)},
    ).mappings().first()
    if row is None:
        raise RuntimeError("session ended checkpoint missing during action index")
    payload = row["payload_json"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    artifact = (payload or {}).get("artifact") or {}
    origin_avatar = str(artifact.get("avatar_id") or "")
    for action in _artifact_actions(artifact):
        conn.execute(
            _INDEX_ACTION_SQL,
            _indexed_action_params(org_id, bot_id, action, origin_avatar),
        )


def index_session_ended_actions(
    org_id: str, bot_id: str, artifact: dict[str, Any]
) -> None:
    """Back-fill queued_actions from a session.ended artifact WITHOUT enqueueing
    a Cedric callback — used when the acting avatar's Slack switch is explicitly
    off. The per-avatar `slack` toggle must suppress ONLY the Slack fan-out,
    never the native action list: queued_actions feeds the native executor
    (approve→calendar/gmail) and the dashboard approval queue, so a slack-off
    (native-only) avatar must still get every post-meeting action indexed.

    With no callback row as the first-write-wins anchor, idempotency across a
    finalize retry (where a summarizer-only action is re-minted under a fresh
    action_id) is preserved by skipping any re-extraction whose normalized text
    already exists for this bot. Live captures keep their stable id and simply
    refresh via ON CONFLICT.
    """
    actions = _artifact_actions(artifact)
    if not actions:
        return
    origin_avatar = str((artifact or {}).get("avatar_id") or "")
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                "SELECT action_id, action FROM queued_actions "
                "WHERE org_id=:org_id AND bot_id=:bot_id"
            ),
            {"org_id": org_id, "bot_id": bot_id},
        ).mappings().all()
        known_ids = {str(r["action_id"]) for r in rows}
        known_texts = {
            _norm_action_text(r["action"])
            for r in rows
            if _norm_action_text(r["action"])
        }
        for action in actions:
            action_id = str(action.get("action_id") or "").strip()
            norm = _norm_action_text(
                action.get("item") or action.get("action") or ""
            )
            # A summarizer-only action (id not yet indexed) whose text already
            # exists is a retry re-extraction under a new id — skip the phantom.
            if action_id not in known_ids and norm and norm in known_texts:
                continue
            conn.execute(
                _INDEX_ACTION_SQL,
                _indexed_action_params(org_id, bot_id, action, origin_avatar),
            )
            known_ids.add(action_id)
            if norm:
                known_texts.add(norm)


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
    outbox_id = int(row[0]) if row else None
    if outbox_id is not None and callback.get("event") == "session.ended":
        _index_session_ended_actions(
            conn, str(callback["org_id"]), str(callback["bot_id"]), outbox_id
        )
    return outbox_id


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
        _capture_finalize_lock(conn, org_id, bot_id)
        _require_capture_open(conn, org_id, bot_id)
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
        _capture_finalize_lock(conn, org_id, bot_id)
        _require_capture_open(conn, org_id, bot_id)
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


def begin_action_finalize(
    org_id: str, bot_id: str
) -> list[dict[str, Any]]:
    """Wait for active captures, fence new ones, return canonical snapshot."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        _capture_finalize_lock(conn, org_id, bot_id)
        conn.execute(
            text(
                """
                INSERT INTO action_finalize_state (
                  org_id, bot_id, state, created_at, updated_at
                ) VALUES (
                  :org_id, :bot_id, 'finalizing',
                  clock_timestamp(), clock_timestamp()
                )
                ON CONFLICT (org_id, bot_id) DO UPDATE SET
                  updated_at=clock_timestamp()
                """
            ),
            {"org_id": org_id, "bot_id": bot_id},
        )
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


def _log_entry(event: str, detail: str = "") -> str:
    """One bounded canonical-log entry (distilled one-liners only, never
    transcript content — same discipline as execution_detail)."""
    return json.dumps(
        {"event": str(event or "")[:40], "detail": str(detail or "")[:200]},
        separators=(",", ":"),
    )


# Bounded jsonb append: at the cap the oldest entry (index 0) drops first, so
# the log can never grow without bound on a retried/edited action.
_APPEND_LOG_SQL = (
    "logs_json=CASE WHEN jsonb_array_length(logs_json) >= 50 "
    "THEN (logs_json - 0) || CAST(:log_entry AS jsonb) "
    "ELSE logs_json || CAST(:log_entry AS jsonb) END"
)


def set_action_status(
    org_id: str,
    action_id: str,
    status: str,
    detail: str = "",
    receipt: Optional[dict] = None,
) -> bool:
    """Persist one org's monotonic execution state on its durable action.

    ``receipt`` (optional, terminal statuses) is the structured receipt the
    canonical Action carries — {"kind": ..., "ref": url/id} — alongside the
    human detail string the dashboard chips already render."""
    aid = str(action_id or "").strip()
    state = str(status or "").strip().lower()
    if not aid or state not in _EXECUTION_STATUSES:
        return False
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT execution_status
                FROM queued_actions
                WHERE org_id=:org_id AND action_id=:action_id
                FOR UPDATE
                """
            ),
            {"org_id": org_id, "action_id": aid},
        ).first()
        if row is None:
            return False
        current = str(row[0] or "")
        if current in _TERMINAL_EXECUTION_STATUSES:
            return True
        # Webhook delivery is at-least-once and may be out of order. A late
        # proposed event cannot repaint an already-approved action.
        if _EXECUTION_RANK[state] < _EXECUTION_RANK.get(current, -1):
            return True
        conn.execute(
            text(
                f"""
                UPDATE queued_actions
                SET execution_status=:status,
                    execution_detail=:detail,
                    execution_updated_at=clock_timestamp(),
                    receipt_json=CASE
                      WHEN CAST(:receipt_json AS jsonb) IS NOT NULL
                      THEN CAST(:receipt_json AS jsonb)
                      ELSE receipt_json
                    END,
                    {_APPEND_LOG_SQL},
                    resolved_at=CASE
                      WHEN :terminal THEN clock_timestamp()
                      ELSE resolved_at
                    END,
                    execution_lease_until=CASE
                      WHEN :terminal THEN NULL
                      ELSE execution_lease_until
                    END,
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND action_id=:action_id
                """
            ),
            {
                "org_id": org_id,
                "action_id": aid,
                "status": state,
                "detail": str(detail or "").strip()[:300],
                "receipt_json": json.dumps(
                    receipt, separators=(",", ":"), sort_keys=True
                )
                if isinstance(receipt, dict) and receipt
                else None,
                "log_entry": _log_entry(f"status:{state}", detail),
                "terminal": state in _TERMINAL_EXECUTION_STATUSES,
            },
        )
    return True


def claim_action_execution(
    org_id: str,
    action_id: str,
    *,
    idempotency_key: str = "",
    detail: str = "",
    lease_seconds: int = _EXECUTION_LEASE_SECONDS,
) -> str:
    """Atomically claim the right to execute one approved action.

    The compare-and-set to ``executing`` is what makes double-approval
    single-execution TRUE across App Runner instances and surfaces (the
    per-instance SQLite decision record cannot serialize two instances).
    Returns 'claimed' (this caller executes), 'lost' (someone else holds or
    finished the claim — do NOT execute), or 'missing' (no durable row for
    this org/action — the caller falls back to its local guard, exactly
    today's semantics for never-indexed native actions)."""
    aid = str(action_id or "").strip()
    if not aid:
        return "missing"
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                f"""
                UPDATE queued_actions
                SET execution_status='executing',
                    execution_detail=:detail,
                    execution_updated_at=clock_timestamp(),
                    execution_lease_until=clock_timestamp()
                      + (:lease_seconds * interval '1 second'),
                    idempotency_key=CASE
                      WHEN idempotency_key='' THEN :idempotency_key
                      ELSE idempotency_key
                    END,
                    {_APPEND_LOG_SQL},
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND action_id=:action_id
                  AND execution_status IN
                      ('', 'needs_details', 'proposed', 'approved')
                RETURNING action_id
                """
            ),
            {
                "org_id": org_id,
                "action_id": aid,
                "detail": str(detail or "").strip()[:300],
                "idempotency_key": str(idempotency_key or "").strip()[:128],
                "lease_seconds": max(30, min(int(lease_seconds), 3600)),
                "log_entry": _log_entry("claim", detail),
            },
        ).first()
        if row is not None:
            return "claimed"
        exists = conn.execute(
            text(
                "SELECT 1 FROM queued_actions "
                "WHERE org_id=:org_id AND action_id=:action_id"
            ),
            {"org_id": org_id, "action_id": aid},
        ).first()
    return "lost" if exists is not None else "missing"


def stale_executing(org_id: str) -> list[dict[str, Any]]:
    """One org's actions whose execution claim outlived its lease — the
    process died mid-vendor-call (or an async dispatch thread was lost).
    Read-only; the reconciler decides what each row becomes."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT action_id, typed_json, execution_detail,
                       extract(
                         epoch from clock_timestamp() - execution_lease_until
                       ) AS expired_for
                FROM queued_actions
                WHERE org_id=:org_id AND execution_status='executing'
                  AND execution_lease_until IS NOT NULL
                  AND execution_lease_until <= clock_timestamp()
                ORDER BY execution_lease_until
                LIMIT 20
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
    out = []
    for row in rows:
        item = dict(row)
        typed = item.get("typed_json")
        if isinstance(typed, str):
            try:
                typed = json.loads(typed)
            except ValueError:
                typed = None
        item["typed_json"] = typed if isinstance(typed, dict) else None
        item["expired_for"] = float(item["expired_for"] or 0)
        out.append(item)
    return out


def record_action_decision(
    org_id: str, action_id: str, fields: dict[str, Any]
) -> bool:
    """Durably record THE canonical decision for (org, action). First write
    wins via the primary key — the cross-instance twin of the SQLite
    action_approvals INSERT OR IGNORE. Returns False when a decision row
    already exists (read it back with get_action_decision)."""
    aid = str(action_id or "").strip()
    decision = str(fields.get("decision") or "").strip()
    if not aid or decision not in ("approve", "reject", "respond"):
        return False
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                INSERT INTO action_decisions (
                  org_id, action_id, decision, selected_slot_id,
                  idempotency_key, decided_via, laura_user_id,
                  previous_status, new_status, execution_job_id, blocked_on,
                  decided_at
                ) VALUES (
                  :org_id, :action_id, :decision, :selected_slot_id,
                  :idempotency_key, :decided_via, :laura_user_id,
                  :previous_status, :new_status, :execution_job_id,
                  :blocked_on, clock_timestamp()
                )
                ON CONFLICT (org_id, action_id) DO NOTHING
                RETURNING action_id
                """
            ),
            {
                "org_id": org_id,
                "action_id": aid,
                "decision": decision,
                "selected_slot_id": str(fields.get("selected_slot_id") or "")[:64],
                "idempotency_key": str(fields.get("idempotency_key") or "")[:128],
                "decided_via": str(fields.get("decided_via") or "")[:32],
                "laura_user_id": str(fields.get("laura_user_id") or "")[:64],
                "previous_status": str(fields.get("previous_status") or "")[:24],
                "new_status": str(fields.get("new_status") or "")[:24],
                "execution_job_id": fields.get("execution_job_id"),
                "blocked_on": str(fields.get("blocked_on") or ""),
            },
        ).first()
    return row is not None


def get_action_decision(org_id: str, action_id: str) -> Optional[dict[str, Any]]:
    """The recorded canonical decision for (org, action), or None."""
    aid = str(action_id or "").strip()
    if not aid:
        return None
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT decision, selected_slot_id, idempotency_key,
                       decided_via, laura_user_id, previous_status,
                       new_status, execution_job_id, blocked_on,
                       extract(epoch from decided_at) AS decided_at
                FROM action_decisions
                WHERE org_id=:org_id AND action_id=:action_id
                """
            ),
            {"org_id": org_id, "action_id": aid},
        ).mappings().first()
    return dict(row) if row is not None else None


def list_blocked_action_decisions(org_id: str) -> list[dict[str, Any]]:
    """Approve-decisions in this org still parked behind unmet dependencies
    ([M8]) — the durable twin of store.list_blocked_action_approvals. Tenant
    isolation is the same RLS GUC every read here sets; `blocked_on` empties
    when the approval runs, so the scan stays small."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT action_id, blocked_on
                FROM action_decisions
                WHERE org_id=:org_id AND decision='approve'
                  AND COALESCE(blocked_on, '') NOT IN ('', '[]')
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def set_action_decision_blocked_on(
    org_id: str, action_id: str, blocked_on: str
) -> None:
    """Re-park a dependency-blocked decision on a SHRUNKEN dependency list (or
    clear it with '[]') — the durable twin of
    store.set_action_approval_blocked_on."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                """
                UPDATE action_decisions SET blocked_on=:blocked_on
                WHERE org_id=:org_id AND action_id=:action_id
                """
            ),
            {
                "org_id": org_id,
                "action_id": str(action_id or "").strip(),
                "blocked_on": blocked_on or "",
            },
        )


def set_action_decision_result(
    org_id: str, action_id: str, new_status: str, execution_job_id: str | None
) -> None:
    """Stamp the execution outcome onto the recorded decision (the decision
    itself is immutable; only the result fields settle after dispatch)."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                """
                UPDATE action_decisions
                SET new_status=:new_status,
                    execution_job_id=COALESCE(
                      :execution_job_id, execution_job_id
                    )
                WHERE org_id=:org_id AND action_id=:action_id
                """
            ),
            {
                "org_id": org_id,
                "action_id": str(action_id or "").strip(),
                "new_status": str(new_status or "")[:24],
                "execution_job_id": execution_job_id,
            },
        )


def get_action(org_id: str, action_id: str) -> Optional[dict[str, Any]]:
    """The full durable canonical Action row for (org, action), or None."""
    aid = str(action_id or "").strip()
    if not aid:
        return None
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT action_id, bot_id, action, owner, due,
                       typed_json, params_schema_json, risk, execution_route,
                       origin_avatar, connected_account_json, permission_json,
                       receipt_json, logs_json, idempotency_key,
                       execution_status, execution_detail,
                       COALESCE(
                         extract(epoch from execution_updated_at), 0
                       ) AS execution_updated_at,
                       COALESCE(
                         extract(epoch from execution_lease_until), 0
                       ) AS execution_lease_until,
                       extract(epoch from created_at) AS created_at,
                       extract(epoch from updated_at) AS updated_at
                FROM queued_actions
                WHERE org_id=:org_id AND action_id=:action_id
                """
            ),
            {"org_id": org_id, "action_id": aid},
        ).mappings().first()
    if row is None:
        return None
    out = dict(row)
    for key in ("typed_json", "params_schema_json", "connected_account_json",
                "permission_json", "receipt_json", "logs_json"):
        value = out.get(key)
        if isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except ValueError:
                out[key] = None
    return out


def update_action_params(
    org_id: str, action_id: str, args: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Merge edited safe parameters into the durable typed spec.

    Read-modify-write under FOR UPDATE; refused (None) for terminal or
    currently-executing actions — an edit can never change what a held claim
    is about to run. When the merged spec has no missing required fields the
    status moves needs_details→proposed; both transitions append to the
    canonical log. Returns the updated typed dict."""
    aid = str(action_id or "").strip()
    if not aid or not isinstance(args, dict):
        return None
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT typed_json, execution_status
                FROM queued_actions
                WHERE org_id=:org_id AND action_id=:action_id
                FOR UPDATE
                """
            ),
            {"org_id": org_id, "action_id": aid},
        ).mappings().first()
        if row is None:
            return None
        status = str(row["execution_status"] or "")
        if status in _TERMINAL_EXECUTION_STATUSES or status == "executing":
            return None
        typed = row["typed_json"]
        if isinstance(typed, str):
            typed = json.loads(typed)
        if not isinstance(typed, dict) or not typed.get("type"):
            return None
        merged_args = dict(typed.get("args") or {})
        merged_args.update(args)
        typed = {**typed, "args": merged_args}
        still_missing = action_plane.missing_params(typed)
        new_status = status
        if status in ("", "needs_details"):
            new_status = "needs_details" if still_missing else "proposed"
        conn.execute(
            text(
                f"""
                UPDATE queued_actions
                SET typed_json=CAST(:typed_json AS jsonb),
                    params_schema_json=CAST(:params_schema_json AS jsonb),
                    risk=CASE WHEN risk='' THEN :risk ELSE risk END,
                    execution_status=:new_status,
                    {_APPEND_LOG_SQL},
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND action_id=:action_id
                """
            ),
            {
                "org_id": org_id,
                "action_id": aid,
                "typed_json": json.dumps(
                    typed, separators=(",", ":"), sort_keys=True
                ),
                "params_schema_json": json.dumps(
                    action_plane.params_schema(typed), separators=(",", ":")
                ),
                "risk": action_plane.risk_for(typed),
                "new_status": new_status,
                "log_entry": _log_entry(
                    "params_edited",
                    "fields: " + ", ".join(sorted(str(k) for k in args)),
                ),
            },
        )
    return typed


def resolve_action(
    org_id: str, action_id: str, outcome: str, detail: str = ""
) -> bool:
    """Resolve an existing durable action once; another tenant sees missing."""
    aid = str(action_id or "").strip()
    state = str(outcome or "").strip().lower()
    if not aid or state not in _TERMINAL_EXECUTION_STATUSES:
        return False
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT execution_status
                FROM queued_actions
                WHERE org_id=:org_id AND action_id=:action_id
                FOR UPDATE
                """
            ),
            {"org_id": org_id, "action_id": aid},
        ).first()
        if row is None:
            return False
        current = str(row[0] or "")
        if current in _TERMINAL_EXECUTION_STATUSES:
            # A successful response may be lost after the PG commit. Retrying
            # the same outcome is success; a conflicting terminal is rejected.
            return current == state
        result = conn.execute(
            text(
                """
                UPDATE queued_actions
                SET execution_status=:status,
                    execution_detail=:detail,
                    execution_updated_at=clock_timestamp(),
                    resolved_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND action_id=:action_id
                """
            ),
            {
                "org_id": org_id,
                "action_id": aid,
                "status": state,
                "detail": str(detail or "").strip()[:300],
            },
        )
    return bool(result.rowcount)


def action_statuses(
    org_id: str, action_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Latest durable states for one tenant; FORCE RLS is the final boundary."""
    ids = sorted({str(value or "").strip() for value in action_ids if value})
    if not ids:
        return {}
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT action_id, execution_status AS status,
                       execution_detail AS detail,
                       extract(epoch from execution_updated_at) AS updated_at
                FROM queued_actions
                WHERE org_id=:org_id
                  AND action_id = ANY(CAST(:action_ids AS text[]))
                  AND execution_status <> ''
                """
            ),
            {"org_id": org_id, "action_ids": ids},
        ).mappings().all()
    return {
        str(row["action_id"]): {
            "status": str(row["status"]),
            "detail": str(row["detail"]),
            "updated_at": float(row["updated_at"]),
        }
        for row in rows
    }


def enqueue_callback(callback: dict[str, Any]) -> Optional[int]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, callback["org_id"])
        return _callback_insert(conn, callback)


def session_ended_artifact(org_id: str, bot_id: str) -> dict[str, Any]:
    """Return the first committed distilled artifact for one session."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT payload_json
                FROM callback_outbox
                WHERE org_id=:org_id AND bot_id=:bot_id
                  AND event='session.ended'
                ORDER BY id
                LIMIT 1
                """
            ),
            {"org_id": org_id, "bot_id": bot_id},
        ).mappings().first()
    if row is None:
        raise RuntimeError("session ended checkpoint missing")
    payload = row["payload_json"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    artifact = (payload or {}).get("artifact")
    if not isinstance(artifact, dict):
        raise RuntimeError("session ended artifact checkpoint invalid")
    return dict(artifact)


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
