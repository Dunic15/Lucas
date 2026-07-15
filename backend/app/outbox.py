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
_ACTION_SETTLE_SECONDS = 5.0


class OutboxUnavailable(RuntimeError):
    """Configured durable store is unavailable; callers must fail closed."""


class ActionCaptureClosed(RuntimeError):
    """The terminal finalizer won the per-bot capture/finalize fence."""


def _pg_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except OutboxUnavailable:
        raise
    except outbox_pg.ActionCaptureClosed as exc:
        raise ActionCaptureClosed(str(exc)) from exc
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
                source_event_key TEXT NOT NULL DEFAULT '',
                source_fingerprint TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (org_id, action_id)
            );
            CREATE INDEX IF NOT EXISTS idx_queued_actions_bot
                ON queued_actions(org_id, bot_id, created_at);

            CREATE TABLE IF NOT EXISTS action_finalize_state (
                org_id TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'finalizing',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (org_id, bot_id)
            );

            CREATE TABLE IF NOT EXISTS action_capture_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                source_event_key TEXT NOT NULL DEFAULT '',
                source_fingerprint TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY (org_id, action_id)
                    REFERENCES queued_actions(org_id, action_id)
                    ON DELETE CASCADE
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_action_capture_events_source
                ON action_capture_events(org_id, action_id, source_event_key)
                WHERE source_event_key <> '';
            CREATE INDEX IF NOT EXISTS idx_action_capture_events_fingerprint
                ON action_capture_events(
                    org_id, action_id, source_fingerprint, created_at DESC
                )
                WHERE source_fingerprint <> '';

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
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(queued_actions)").fetchall()
        }
        if "source_event_key" not in columns:
            conn.execute(
                "ALTER TABLE queued_actions "
                "ADD COLUMN source_event_key TEXT NOT NULL DEFAULT ''"
            )
        if "source_fingerprint" not in columns:
            conn.execute(
                "ALTER TABLE queued_actions "
                "ADD COLUMN source_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        conn.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_queued_actions_source_event
                ON queued_actions(org_id, source_event_key)
                WHERE source_event_key <> '';
            CREATE INDEX IF NOT EXISTS idx_queued_actions_fingerprint
                ON queued_actions(
                    org_id, bot_id, source_fingerprint, created_at DESC
                )
                WHERE source_fingerprint <> '';
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


def _slack_capability_off(avatar_id: str | None) -> bool:
    """True ONLY when the acting avatar's ``slack`` switch is EXPLICITLY off.

    This is the capture-side twin of the direct Slack seams gated in #221
    (``main.deliver_artifact`` / ``autopilot.maybe_deliver``): the Cedric
    callback-outbox is Cedric's Slack broker, so an avatar whose owner turned
    Slack OFF must not fan a callback out to it. An UNSET/True switch, a
    missing/empty ``avatar_id``, or any store hiccup all fall through to
    deliver-as-before — fail-open, matching #221, so a store blip can never
    silently drop a legitimate callback. Only an explicit ``False`` suppresses.
    Reads the avatar-keyed switch only; no transcript/PII is touched.
    """
    aid = str(avatar_id or "").strip()
    if not aid:
        return False
    try:
        return store.get_avatar_capabilities(aid).get("slack") is False
    except Exception:  # noqa: BLE001 — fail-open: delivery beats a store blip
        return False


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


def _callback_record(session: Any, item: dict) -> tuple[str, dict | None]:
    integration = dict(getattr(session, "integration", None) or {})
    org_id, team, channel, ref = _routing(
        {**integration, "org_id": getattr(session, "org_id", "")}
    )
    action_id = str((item or {}).get("action_id") or "").strip()
    callback_record = None
    # Capability gate (migration-free): suppress the Cedric callback when the
    # acting avatar's Slack switch is explicitly OFF. session.avatar_id is known
    # here, so both backends stay ungated of avatar_id — the queued_action still
    # persists below/at the caller; only the Slack fan-out is skipped.
    if (
        action_id
        and str(integration.get("callback_url") or "").strip()
        and not _slack_capability_off(getattr(session, "avatar_id", ""))
    ):
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
            "not_before_seconds": _ACTION_SETTLE_SECONDS,
        }
    return org_id, callback_record


def persist_action_capture_once(
    session: Any,
    item: dict,
    *,
    source_event_key: str = "",
    source_fingerprint: str = "",
    dedupe_window_seconds: float = 30.0,
) -> tuple[dict, bool, int | None]:
    """Return (canonical item, created, outbox id) after one durable tx."""
    org_id, callback_record = _callback_record(session, item)
    event_key = str(source_event_key or "")[:128]
    fingerprint = str(source_fingerprint or "")[:128]
    window = max(1.0, min(float(dedupe_window_seconds), 300.0))
    if control_plane.enabled():
        return _pg_call(
            outbox_pg.persist_action_capture_once,
            org_id,
            str(session.bot_id),
            item,
            callback_record,
            source_event_key=event_key,
            source_fingerprint=fingerprint,
            dedupe_window_seconds=window,
        )

    _ensure_schema()
    now = time.time()
    with store._LOCK, store._connect() as conn:
        closed = conn.execute(
            "SELECT state FROM action_finalize_state "
            "WHERE org_id=? AND bot_id=?",
            (org_id, str(session.bot_id)),
        ).fetchone()
        if closed is not None:
            raise ActionCaptureClosed(str(closed["state"]))
        existing = None
        if event_key:
            existing = conn.execute(
                """
                SELECT action_id, action, owner, due
                FROM queued_actions
                WHERE org_id=? AND source_event_key=?
                LIMIT 1
                """,
                (org_id, event_key),
            ).fetchone()
        elif fingerprint:
            existing = conn.execute(
                """
                SELECT action_id, action, owner, due
                FROM queued_actions
                WHERE org_id=? AND bot_id=? AND source_fingerprint=?
                  AND created_at >= ?
                ORDER BY created_at DESC, action_id
                LIMIT 1
                """,
                (org_id, str(session.bot_id), fingerprint, now - window),
            ).fetchone()
        if existing is not None:
            return dict(existing), False, None

        canonical = {
            "action_id": str(item.get("action_id") or ""),
            "action": str(item.get("action") or "")[:300],
            "owner": str(item.get("owner") or "")[:100],
            "due": str(item.get("due") or "")[:100],
        }
        conn.execute(
            """
            INSERT INTO queued_actions (
                org_id, bot_id, action_id, action, owner, due,
                source_event_key, source_fingerprint, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                org_id, str(session.bot_id), canonical["action_id"],
                canonical["action"], canonical["owner"], canonical["due"],
                event_key, fingerprint, now, now,
            ),
        )
        outbox_id = None
        if callback_record:
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
                    callback_record["idempotency_key"], org_id,
                    callback_record["bot_id"], callback_record["action_id"],
                    callback_record["event"], callback_record["callback_url"],
                    callback_record["team_id"], callback_record["channel"],
                    json.dumps(
                        callback_record["external_ref"],
                        separators=(",", ":"), sort_keys=True,
                    ),
                    json.dumps(
                        callback_record["payload"],
                        separators=(",", ":"), sort_keys=True,
                    ),
                    now + float(
                        callback_record.get("not_before_seconds") or 0
                    ),
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id FROM callback_outbox
                WHERE org_id=? AND idempotency_key=?
                """,
                (org_id, callback_record["idempotency_key"]),
            ).fetchone()
            outbox_id = int(row["id"]) if row else None
        return canonical, True, outbox_id


def persist_action_capture(session: Any, item: dict) -> int | None:
    """Compatibility wrapper for callers without producer identity."""
    _item, _created, outbox_id = persist_action_capture_once(session, item)
    return outbox_id


def extend_action_capture_once(
    session: Any,
    item: dict,
    fragment: str,
    *,
    source_event_key: str = "",
    source_fingerprint: str = "",
    dedupe_window_seconds: float = 30.0,
) -> tuple[dict, bool]:
    """Append a continuation once; queued action + wire payload share one tx."""
    org_id = str(getattr(session, "org_id", "") or settings.demo_org_id)
    action_id = str((item or {}).get("action_id") or "")
    event_key = str(source_event_key or "")[:128]
    fingerprint = str(source_fingerprint or "")[:128]
    window = max(1.0, min(float(dedupe_window_seconds), 300.0))
    if control_plane.enabled():
        return _pg_call(
            outbox_pg.extend_action_capture_once,
            org_id,
            str(session.bot_id),
            action_id,
            fragment,
            source_event_key=event_key,
            source_fingerprint=fingerprint,
            dedupe_window_seconds=window,
        )

    _ensure_schema()
    now = time.time()
    with store._LOCK, store._connect() as conn:
        closed = conn.execute(
            "SELECT state FROM action_finalize_state "
            "WHERE org_id=? AND bot_id=?",
            (org_id, str(session.bot_id)),
        ).fetchone()
        if closed is not None:
            raise ActionCaptureClosed(str(closed["state"]))
        row = conn.execute(
            """
            SELECT action_id, action, owner, due
            FROM queued_actions
            WHERE org_id=? AND bot_id=? AND action_id=?
            """,
            (org_id, str(session.bot_id), action_id),
        ).fetchone()
        if row is None:
            raise OutboxUnavailable("queued action missing during continuation")
        duplicate = None
        if event_key:
            duplicate = conn.execute(
                """
                SELECT 1 FROM action_capture_events
                WHERE org_id=? AND action_id=? AND source_event_key=?
                LIMIT 1
                """,
                (org_id, action_id, event_key),
            ).fetchone()
        elif fingerprint:
            duplicate = conn.execute(
                """
                SELECT 1 FROM action_capture_events
                WHERE org_id=? AND action_id=? AND source_fingerprint=?
                  AND created_at >= ?
                LIMIT 1
                """,
                (org_id, action_id, fingerprint, now - window),
            ).fetchone()
        if duplicate is not None:
            return dict(row), False

        updated = " ".join(
            (str(row["action"]) + " " + str(fragment or "")).split()
        )[:300]
        conn.execute(
            """
            INSERT INTO action_capture_events (
              org_id, action_id, source_event_key,
              source_fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (org_id, action_id, event_key, fingerprint, now),
        )
        conn.execute(
            """
            UPDATE queued_actions SET action=?, updated_at=?
            WHERE org_id=? AND bot_id=? AND action_id=?
            """,
            (updated, now, org_id, str(session.bot_id), action_id),
        )
        callback = conn.execute(
            """
            UPDATE callback_outbox
            SET payload_json=json_set(payload_json, '$.action', ?),
                next_attempt_at=MAX(next_attempt_at, ?)
            WHERE org_id=? AND action_id=? AND event='action.requested'
              AND status IN ('pending', 'failed')
            """,
            (updated, now + 1.0, org_id, action_id),
        )
        if callback.rowcount == 0:
            status = conn.execute(
                """
                SELECT status FROM callback_outbox
                WHERE org_id=? AND action_id=? AND event='action.requested'
                """,
                (org_id, action_id),
            ).fetchone()
            if status is not None:
                raise OutboxUnavailable("action callback escaped settle fence")
        return {
            "action_id": str(row["action_id"]),
            "action": updated,
            "owner": str(row["owner"]),
            "due": str(row["due"]),
        }, True


def begin_action_finalize(org_id: str, bot_id: str) -> list[dict]:
    """Drain active captures and atomically reject every later capture."""
    org = org_id or settings.demo_org_id
    if control_plane.enabled():
        return _pg_call(outbox_pg.begin_action_finalize, org, bot_id)
    _ensure_schema()
    now = time.time()
    with store._LOCK, store._connect() as conn:
        conn.execute(
            """
            INSERT INTO action_finalize_state (
              org_id, bot_id, state, created_at, updated_at
            ) VALUES (?, ?, 'finalizing', ?, ?)
            ON CONFLICT(org_id, bot_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (org, bot_id, now, now),
        )
        rows = conn.execute(
            """
            SELECT action_id, action, owner, due
            FROM queued_actions
            WHERE org_id=? AND bot_id=?
            ORDER BY created_at, action_id
            """,
            (org, bot_id),
        ).fetchall()
    return [dict(row) for row in rows]


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
    bot_id: str, action_id: str, payload: dict, avatar_id: str = "",
) -> int | None:
    callback_url = str((integration or {}).get("callback_url") or "").strip()
    if not callback_url:
        return None
    # Same capture-side gate as _callback_record, for the reconcile heal path
    # (enqueue_action_requested), which takes integration/bot_id rather than a
    # session. avatar_id is threaded from session.avatar_id; unset/absent →
    # deliver as before (fail-open). session.ended gates in enqueue_session_ended
    # (it must still index queued_actions), so it never passes avatar_id here.
    if _slack_capability_off(avatar_id):
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
                "not_before_seconds": (
                    _ACTION_SETTLE_SECONDS
                    if event == "action.requested" else 0.0
                ),
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
                (
                    now + _ACTION_SETTLE_SECONDS
                    if event == "action.requested" else now
                ),
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM callback_outbox WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
    return int(row["id"]) if row else None


def enqueue_action_requested(
    integration: dict, bot_id: str, item: dict, *, avatar_id: str = ""
) -> int | None:
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
        avatar_id=avatar_id,
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
    # Capability gate (migration-free): a slack-OFF avatar suppresses the Cedric
    # fan-out but NEVER the native action list. On Postgres the session.ended
    # actions are STILL indexed into queued_actions (the native executor +
    # dashboard approval queue read them); only the deliverable callback row is
    # skipped, so no callback_outbox row / avatar_id column is needed. The SQLite
    # path never indexed from session.ended, so there is nothing extra to keep.
    if _slack_capability_off(str((artifact or {}).get("avatar_id") or "")):
        if control_plane.enabled():
            _pg_call(
                outbox_pg.index_session_ended_actions,
                org_id, bot_id, dict(artifact or {}),
            )
        return None
    return _enqueue(
        event="session.ended",
        idempotency_key=f"session.ended:{bot_id}",
        integration=integration,
        bot_id=bot_id,
        action_id="",
        payload=payload,
    )


def _session_ended_artifact(org_id: str, bot_id: str) -> dict:
    if control_plane.enabled():
        return _pg_call(
            outbox_pg.session_ended_artifact,
            org_id or settings.demo_org_id,
            bot_id,
        )
    _ensure_schema()
    with store._LOCK, store._connect() as conn:
        row = conn.execute(
            """
            SELECT payload_json FROM callback_outbox
            WHERE org_id=? AND bot_id=? AND event='session.ended'
            ORDER BY id LIMIT 1
            """,
            (org_id or settings.demo_org_id, bot_id),
        ).fetchone()
    if row is None:
        raise OutboxUnavailable("session ended checkpoint missing")
    payload = json.loads(row["payload_json"])
    artifact = (payload or {}).get("artifact")
    if not isinstance(artifact, dict):
        raise OutboxUnavailable("session ended artifact checkpoint invalid")
    return dict(artifact)


def checkpoint_session_ended(
    integration: dict, bot_id: str, artifact: dict
) -> dict:
    """Commit once, then return the canonical first wire artifact."""
    org_id, _team, _channel, _ref = _routing(integration)
    outbox_id = enqueue_session_ended(integration, bot_id, artifact)
    if outbox_id is None:
        # The only non-error way to get here is the capability gate: the avatar's
        # Slack switch is explicitly OFF, so the session.ended fan-out to Cedric
        # is intentionally suppressed. No durable row was committed, so the wire
        # artifact passed in IS canonical — return it rather than failing
        # finalize. (A genuine store failure raises OutboxUnavailable upstream.)
        if _slack_capability_off(str((artifact or {}).get("avatar_id") or "")):
            return dict(artifact)
        raise OutboxUnavailable("session ended callback was not checkpointed")
    return _session_ended_artifact(org_id, bot_id)


def reconcile_sessions() -> int:
    """Heal a crash between durable action capture and outbox enqueue."""
    count = 0
    for session in store.all_sessions():
        if not session.integration or not session.integration.get("callback_url"):
            continue
        for item in queued_actions(session.org_id, session.bot_id):
            if enqueue_action_requested(
                dict(session.integration), session.bot_id, item,
                avatar_id=getattr(session, "avatar_id", ""),
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
    # Per-avatar `slack` capability is enforced at ENQUEUE (see
    # _slack_capability_off, applied in _callback_record / _enqueue): a row only
    # exists here if the acting avatar's Slack switch was unset/True/absent when
    # captured. Gating at capture — where session.avatar_id is known — keeps this
    # delivery path (and callback_outbox) free of an avatar_id column, so no
    # schema change or migration is needed on either the SQLite or Postgres path.
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
        # Only durable (uuid) orgs have a Postgres outbox; a session-shaped org
        # (u_<hash>) would crash the callback_outbox RLS uuid cast and has no
        # SQLite outbox either (its schema is control-plane-gated). No deliveries.
        if not control_plane.is_durable_org(org_id or settings.demo_org_id):
            return []
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
