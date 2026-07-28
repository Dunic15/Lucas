"""OpenClaw run ledger, gateway handoff, and tool bridge.

This module keeps the POC isolated from Laura's legacy executors. It owns the
OpenClaw run state and uses the existing canonical Action ledger only for the
approval/exactly-once/receipt surface.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .. import control_plane, native_runtime, pipedream_client, pipedream_executor, store
from ..actions import action_plane, executor, ledger
from ..config import settings
from . import gates

RUN_STATUSES = (
    "queued", "planning", "running", "needs_attention", "done", "failed",
    "cancelled",
)
ACTION_RUN_STATUSES = RUN_STATUSES
TERMINAL_RUN_STATUSES = ("needs_attention", "done", "failed", "cancelled")
SIDE_EFFECT_TOOLS = {
    "gmail_send",
    "calendar_create_event",
    "asana_create_task",
    "asana_update_task",
    "asana_add_comment",
    "pipedream_run_app_action",
    "browser_fallback",
}
READ_TOOLS = {
    "laura_connected_tools",
    "pipedream_list_accounts",
    "pipedream_list_app_actions",
}
ALL_TOOLS = tuple(sorted(SIDE_EFFECT_TOOLS | READ_TOOLS))
_ACTION_TOOL_BY_TYPE = {
    "email.send": "gmail_send",
    "calendar.create_event": "calendar_create_event",
    "asana.create_task": "asana_create_task",
    "asana.update_task": "asana_update_task",
    "asana.add_comment": "asana_add_comment",
    "browser.manual": "browser_fallback",
}
_MAX_OPENRESPONSES_TURNS = 32
_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, separators=(",", ":"))


def _json_loads(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:  # noqa: BLE001 — old/bad rows should not break dashboard
        return {} if default is None else default


def _iso(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _pg(org_id: str) -> bool:
    return bool(control_plane.enabled() and control_plane.is_durable_org(org_id))


def _ensure_sqlite_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        with store._LOCK, store._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS openclaw_runs (
                    org_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    meeting_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    gateway_run_id TEXT DEFAULT '',
                    input_json TEXT DEFAULT '{}',
                    metrics_json TEXT DEFAULT '{}',
                    error TEXT DEFAULT '',
                    started_at REAL,
                    finished_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (org_id, run_id),
                    UNIQUE (org_id, meeting_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS openclaw_action_runs (
                    org_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    goal TEXT DEFAULT '',
                    result_summary TEXT DEFAULT '',
                    receipt_json TEXT DEFAULT '{}',
                    tools_json TEXT DEFAULT '[]',
                    error TEXT DEFAULT '',
                    started_at REAL,
                    finished_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (org_id, run_id, action_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS openclaw_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    action_id TEXT DEFAULT '',
                    kind TEXT NOT NULL,
                    summary TEXT DEFAULT '',
                    tool TEXT DEFAULT '',
                    status TEXT DEFAULT '',
                    safe_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS openclaw_tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    idempotency_key TEXT DEFAULT '',
                    tool TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT DEFAULT '{}',
                    result_json TEXT DEFAULT '{}',
                    error TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (org_id, run_id, action_id, step_id)
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                  openclaw_tool_calls_action_once
                ON openclaw_tool_calls(org_id, run_id, action_id)
                """
            )
        _SCHEMA_READY = True


def _pg_fetchone(conn, sql: str, params: dict) -> dict | None:
    row = conn.execute(_text(sql), params).mappings().first()
    return dict(row) if row is not None else None


def _pg_fetchall(conn, sql: str, params: dict) -> list[dict]:
    return [dict(r) for r in conn.execute(_text(sql), params).mappings().all()]


def _text(sql: str):
    from sqlalchemy import text

    return text(sql)


def _run_row(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "org_id": str(row.get("org_id") or ""),
        "run_id": str(row.get("run_id") or ""),
        "meeting_id": str(row.get("meeting_id") or ""),
        "status": str(row.get("status") or ""),
        "gateway_run_id": str(row.get("gateway_run_id") or ""),
        "input": _json_loads(row.get("input_json"), {}),
        "metrics": _json_loads(row.get("metrics_json"), {}),
        "error": str(row.get("error") or ""),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _action_row(row: dict) -> dict:
    return {
        "org_id": str(row.get("org_id") or ""),
        "run_id": str(row.get("run_id") or ""),
        "action_id": str(row.get("action_id") or ""),
        "status": str(row.get("status") or ""),
        "goal": str(row.get("goal") or ""),
        "result_summary": str(row.get("result_summary") or ""),
        "receipt": _json_loads(row.get("receipt_json"), {}),
        "tools": _json_loads(row.get("tools_json"), []),
        "error": str(row.get("error") or ""),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _event_row(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "org_id": str(row.get("org_id") or ""),
        "run_id": str(row.get("run_id") or ""),
        "action_id": str(row.get("action_id") or ""),
        "kind": str(row.get("kind") or ""),
        "summary": str(row.get("summary") or ""),
        "tool": str(row.get("tool") or ""),
        "status": str(row.get("status") or ""),
        "safe": _json_loads(row.get("safe_json"), {}),
        "created_at": _iso(row.get("created_at")),
    }


def _event(org_id: str, run_id: str, kind: str, summary: str = "", *,
           action_id: str = "", tool: str = "", status: str = "",
           safe: dict | None = None) -> None:
    if not org_id or not run_id:
        return
    safe_json = _json_dumps(safe or {})
    now = _now()
    if _pg(org_id):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            conn.execute(
                _text(
                    """
                    INSERT INTO openclaw_events
                      (org_id, run_id, action_id, kind, summary, tool, status,
                       safe_json, created_at)
                    VALUES
                      (:org_id, :run_id, :action_id, :kind, :summary, :tool,
                       :status, CAST(:safe_json AS jsonb), clock_timestamp())
                    """
                ),
                {
                    "org_id": org_id,
                    "run_id": run_id,
                    "action_id": action_id,
                    "kind": kind,
                    "summary": summary[:500],
                    "tool": tool[:80],
                    "status": status[:40],
                    "safe_json": safe_json,
                },
            )
        return
    _ensure_sqlite_schema()
    with store._LOCK, store._connect() as conn:
        conn.execute(
            """
            INSERT INTO openclaw_events
              (org_id, run_id, action_id, kind, summary, tool, status, safe_json,
               created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                org_id, run_id, action_id, kind, summary[:500], tool[:80],
                status[:40], safe_json, now,
            ),
        )


def _status_row_counts(org_id: str, run_id: str) -> dict[str, int]:
    if _pg(org_id):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            rows = conn.execute(
                _text(
                    """
                    SELECT status, count(*) AS n
                    FROM openclaw_action_runs
                    WHERE org_id=:org_id AND run_id=:run_id
                    GROUP BY status
                    """
                ),
                {"org_id": org_id, "run_id": run_id},
            ).all()
        return {str(r[0] or ""): int(r[1] or 0) for r in rows}
    _ensure_sqlite_schema()
    with store._LOCK, store._connect() as conn:
        rows = conn.execute(
            """
            SELECT status, count(*) AS n
            FROM openclaw_action_runs
            WHERE org_id=? AND run_id=?
            GROUP BY status
            """,
            (org_id, run_id),
        ).fetchall()
    return {str(r["status"] or ""): int(r["n"] or 0) for r in rows}


def _sanitize_action(action: dict, meeting_id: str, index: int) -> dict:
    if not isinstance(action, dict):
        action = {"item": str(action or "")}
    aid = str(action.get("action_id") or "").strip()
    if not aid:
        seed = f"{meeting_id}:{index}:{action.get('item') or action.get('action') or ''}"
        aid = "oc_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    typed = action.get("typed") if isinstance(action.get("typed"), dict) else {}
    return {
        "action_id": aid,
        "action": str(action.get("action") or action.get("item") or "")[:600],
        "owner": str(action.get("owner") or "")[:200],
        "deadline": str(action.get("deadline") or action.get("due") or "")[:120],
        "typed": {
            "type": str((typed or {}).get("type") or ""),
            "args": dict((typed or {}).get("args") or {}),
        },
        "execution_route": "openclaw",
        "risk": action_plane.risk_for(typed),
        "missing_params": action_plane.missing_params(typed),
    }


def _participants_from_artifact(artifact: dict) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    avatar = str(artifact.get("avatar_id") or "").casefold()
    for line in str(artifact.get("transcript") or "").splitlines():
        name, sep, _ = line.partition(":")
        name = name.strip()
        key = name.casefold()
        if sep and key and key != avatar and key not in seen:
            seen.add(key)
            names.append(name[:120])
    return names[:50]


def tool_catalog(org_id: str) -> list[dict]:
    """Connection-aware tool catalog sent to OpenClaw; no secrets or transcripts."""
    org = str(org_id or "").strip()
    out: list[dict] = []
    for item in native_runtime.catalog(org):
        action_type = str(item.get("type") or "")
        app = pipedream_executor.app_for_type(action_type)
        out.append(
            {
                "tool": {
                    "calendar.create_event": "calendar_create_event",
                    "email.send": "gmail_send",
                    "asana.create_task": "asana_create_task",
                    "asana.update_task": "asana_update_task",
                    "asana.add_comment": "asana_add_comment",
                }.get(action_type, action_type),
                "action_type": action_type,
                "family": item.get("family") or "",
                "label": item.get("name") or action_type,
                "connected": bool(item.get("connected")),
                "write": bool(item.get("write")),
                "source": "native",
                "app": app,
            }
        )
    for action_type in sorted(pipedream_executor.action_types()):
        app = pipedream_executor.app_for_type(action_type)
        connected = False
        try:
            connected = pipedream_executor.app_connected(org, app)
        except Exception:  # noqa: BLE001 — catalog is best-effort
            connected = False
        out.append(
            {
                "tool": "pipedream_run_app_action",
                "action_type": action_type,
                "family": app,
                "label": action_type,
                "connected": connected,
                "write": True,
                "source": "pipedream",
                "app": app,
            }
        )
    if settings.pipedream_executor and pipedream_client.enabled():
        out.append(
            {
                "tool": "pipedream_run_app_action",
                "action_type": "pd.<app>.run",
                "family": "long_tail",
                "label": "Pipedream pre-built action",
                "connected": True,
                "write": True,
                "source": "pipedream",
                "app": "*",
            }
        )
    out.append(
        {
            "tool": "browser_fallback",
            "action_type": "browser.manual",
            "family": "browser",
            "label": "Browser fallback",
            "connected": bool(settings.openclaw_browser_enabled),
            "write": False,
            "source": "openclaw",
            "app": "browser",
        }
    )
    return out


def _run_input(bot_id: str, artifact: dict, org_id: str) -> dict:
    actions = [
        _sanitize_action(a, bot_id, i)
        for i, a in enumerate((artifact or {}).get("actions") or [], 1)
    ]
    return {
        "schema_version": "openclaw-poc-m0-m7",
        "org_id": org_id,
        "meeting_id": bot_id,
        "avatar_id": str((artifact or {}).get("avatar_id") or ""),
        "meeting_type": str((artifact or {}).get("meeting_type") or ""),
        "summary": str((artifact or {}).get("summary") or "")[:8000],
        "decisions": [
            str(d)[:1000] for d in ((artifact or {}).get("decisions") or [])[:80]
        ],
        "actions": actions,
        "participants": _participants_from_artifact(artifact or {}),
        "tool_catalog": tool_catalog(org_id),
        "raw_transcript_included": False,
    }


def _insert_run(org_id: str, meeting_id: str, payload: dict) -> tuple[dict, bool]:
    run_id = "oc_" + uuid.uuid4().hex
    now = _now()
    if _pg(org_id):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            inserted = conn.execute(
                _text(
                    """
                    INSERT INTO openclaw_runs
                      (org_id, run_id, meeting_id, status, input_json,
                       metrics_json, created_at, updated_at)
                    VALUES
                      (:org_id, :run_id, :meeting_id, 'queued',
                       CAST(:input_json AS jsonb), '{}'::jsonb,
                       clock_timestamp(), clock_timestamp())
                    ON CONFLICT (org_id, meeting_id) DO NOTHING
                    """
                ),
                {
                    "org_id": org_id,
                    "run_id": run_id,
                    "meeting_id": meeting_id,
                    "input_json": _json_dumps(payload),
                },
            )
            row = _pg_fetchone(
                conn,
                """
                SELECT * FROM openclaw_runs
                WHERE org_id=:org_id AND meeting_id=:meeting_id
                """,
                {"org_id": org_id, "meeting_id": meeting_id},
            )
        return _run_row(row) or {}, inserted.rowcount == 1
    _ensure_sqlite_schema()
    with store._LOCK, store._connect() as conn:
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO openclaw_runs
              (org_id, run_id, meeting_id, status, input_json, metrics_json,
               created_at, updated_at)
            VALUES (?, ?, ?, 'queued', ?, '{}', ?, ?)
            """,
            (org_id, run_id, meeting_id, _json_dumps(payload), now, now),
        )
        row = conn.execute(
            "SELECT * FROM openclaw_runs WHERE org_id=? AND meeting_id=?",
            (org_id, meeting_id),
        ).fetchone()
    return _run_row(dict(row) if row else None) or {}, inserted.rowcount == 1


def _insert_action_runs(org_id: str, run_id: str, actions: list[dict]) -> None:
    if not actions:
        return
    now = _now()
    if _pg(org_id):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            for action in actions:
                conn.execute(
                    _text(
                        """
                        INSERT INTO openclaw_action_runs
                          (org_id, run_id, action_id, status, goal,
                           receipt_json, tools_json, created_at, updated_at)
                        VALUES
                          (:org_id, :run_id, :action_id, 'queued', :goal,
                           '{}'::jsonb, '[]'::jsonb,
                           clock_timestamp(), clock_timestamp())
                        ON CONFLICT (org_id, run_id, action_id) DO NOTHING
                        """
                    ),
                    {
                        "org_id": org_id,
                        "run_id": run_id,
                        "action_id": action["action_id"],
                        "goal": action["action"][:600],
                    },
                )
        return
    _ensure_sqlite_schema()
    with store._LOCK, store._connect() as conn:
        for action in actions:
            conn.execute(
                """
                INSERT OR IGNORE INTO openclaw_action_runs
                  (org_id, run_id, action_id, status, goal, receipt_json,
                   tools_json, created_at, updated_at)
                VALUES (?, ?, ?, 'queued', ?, '{}', '[]', ?, ?)
                """,
                (org_id, run_id, action["action_id"], action["action"][:600], now, now),
            )


def get_run(org_id: str, run_id: str) -> dict | None:
    org = str(org_id or "").strip()
    rid = str(run_id or "").strip()
    if not (org and rid):
        return None
    if _pg(org):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org)
            row = _pg_fetchone(
                conn,
                "SELECT * FROM openclaw_runs WHERE org_id=:org_id AND run_id=:run_id",
                {"org_id": org, "run_id": rid},
            )
        return _run_row(row)
    _ensure_sqlite_schema()
    with store._LOCK, store._connect() as conn:
        row = conn.execute(
            "SELECT * FROM openclaw_runs WHERE org_id=? AND run_id=?",
            (org, rid),
        ).fetchone()
    return _run_row(dict(row) if row else None)


def list_runs(org_id: str, limit: int = 20) -> list[dict]:
    org = str(org_id or "").strip()
    if not org:
        return []
    limit = max(1, min(int(limit or 20), 100))
    if _pg(org):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org)
            rows = _pg_fetchall(
                conn,
                """
                SELECT * FROM openclaw_runs
                WHERE org_id=:org_id
                ORDER BY created_at DESC
                LIMIT :limit
                """,
                {"org_id": org, "limit": limit},
            )
    else:
        _ensure_sqlite_schema()
        with store._LOCK, store._connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM openclaw_runs
                    WHERE org_id=?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (org, limit),
                ).fetchall()
            ]
    out: list[dict] = []
    for row in rows:
        shaped = _run_row(row) or {}
        shaped["action_counts"] = _status_row_counts(org, shaped.get("run_id", ""))
        out.append(shaped)
    return out


def run_detail(org_id: str, run_id: str) -> dict | None:
    run = get_run(org_id, run_id)
    if not run:
        return None
    org = str(org_id or "").strip()
    rid = str(run_id or "").strip()
    if _pg(org):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org)
            actions = _pg_fetchall(
                conn,
                """
                SELECT * FROM openclaw_action_runs
                WHERE org_id=:org_id AND run_id=:run_id
                ORDER BY created_at ASC
                """,
                {"org_id": org, "run_id": rid},
            )
            events = _pg_fetchall(
                conn,
                """
                SELECT * FROM openclaw_events
                WHERE org_id=:org_id AND run_id=:run_id
                ORDER BY created_at DESC, id DESC
                LIMIT 200
                """,
                {"org_id": org, "run_id": rid},
            )
    else:
        _ensure_sqlite_schema()
        with store._LOCK, store._connect() as conn:
            actions = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM openclaw_action_runs
                    WHERE org_id=? AND run_id=?
                    ORDER BY created_at ASC
                    """,
                    (org, rid),
                ).fetchall()
            ]
            events = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM openclaw_events
                    WHERE org_id=? AND run_id=?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 200
                    """,
                    (org, rid),
                ).fetchall()
            ]
    run["actions"] = [_action_row(r) for r in actions]
    run["events"] = [_event_row(r) for r in events]
    run["tool_catalog"] = (run.get("input") or {}).get("tool_catalog") or []
    return run


def _update_run(org_id: str, run_id: str, status: str, *, error: str = "",
                gateway_run_id: str = "", metrics: dict | None = None) -> None:
    state = status if status in RUN_STATUSES else "failed"
    terminal = state in TERMINAL_RUN_STATUSES
    now = _now()
    if _pg(org_id):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            conn.execute(
                _text(
                    """
                    UPDATE openclaw_runs
                    SET status=:status,
                        gateway_run_id=COALESCE(NULLIF(:gateway_run_id, ''), gateway_run_id),
                        error=:error,
                        metrics_json=CASE
                          WHEN CAST(:metrics_json AS jsonb) IS NOT NULL
                          THEN CAST(:metrics_json AS jsonb)
                          ELSE metrics_json
                        END,
                        started_at=CASE
                          WHEN started_at IS NULL
                           AND :status IN ('planning', 'running')
                          THEN clock_timestamp()
                          ELSE started_at
                        END,
                        finished_at=CASE
                          WHEN :terminal THEN clock_timestamp()
                          ELSE finished_at
                        END,
                        updated_at=clock_timestamp()
                    WHERE org_id=:org_id AND run_id=:run_id
                    """
                ),
                {
                    "org_id": org_id,
                    "run_id": run_id,
                    "status": state,
                    "error": error[:500],
                    "gateway_run_id": gateway_run_id[:160],
                    "metrics_json": _json_dumps(metrics) if metrics else None,
                    "terminal": terminal,
                },
            )
        return
    _ensure_sqlite_schema()
    with store._LOCK, store._connect() as conn:
        conn.execute(
            """
            UPDATE openclaw_runs
            SET status=?,
                gateway_run_id=CASE WHEN ? <> '' THEN ? ELSE gateway_run_id END,
                error=?,
                metrics_json=CASE WHEN ? <> '' THEN ? ELSE metrics_json END,
                started_at=CASE
                    WHEN started_at IS NULL AND ? IN ('planning', 'running')
                    THEN ? ELSE started_at END,
                finished_at=CASE WHEN ? THEN ? ELSE finished_at END,
                updated_at=?
            WHERE org_id=? AND run_id=?
            """,
            (
                state, gateway_run_id, gateway_run_id, error[:500],
                _json_dumps(metrics) if metrics else "",
                _json_dumps(metrics) if metrics else "",
                state, now, 1 if terminal else 0, now, now, org_id, run_id,
            ),
        )


def _set_action_run(org_id: str, run_id: str, action_id: str, status: str, *,
                    summary: str = "", receipt: dict | None = None,
                    tools: list | None = None, error: str = "") -> None:
    state = status if status in ACTION_RUN_STATUSES else "failed"
    terminal = state in TERMINAL_RUN_STATUSES
    now = _now()
    if _pg(org_id):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            conn.execute(
                _text(
                    """
                    UPDATE openclaw_action_runs
                    SET status=:status,
                        result_summary=COALESCE(NULLIF(:summary, ''), result_summary),
                        receipt_json=CASE
                          WHEN CAST(:receipt_json AS jsonb) IS NOT NULL
                          THEN CAST(:receipt_json AS jsonb)
                          ELSE receipt_json
                        END,
                        tools_json=CASE
                          WHEN CAST(:tools_json AS jsonb) IS NOT NULL
                          THEN CAST(:tools_json AS jsonb)
                          ELSE tools_json
                        END,
                        error=:error,
                        started_at=CASE
                          WHEN started_at IS NULL
                           AND :status IN ('planning', 'running')
                          THEN clock_timestamp()
                          ELSE started_at
                        END,
                        finished_at=CASE
                          WHEN :terminal THEN clock_timestamp()
                          ELSE finished_at
                        END,
                        updated_at=clock_timestamp()
                    WHERE org_id=:org_id AND run_id=:run_id AND action_id=:action_id
                    """
                ),
                {
                    "org_id": org_id,
                    "run_id": run_id,
                    "action_id": action_id,
                    "status": state,
                    "summary": summary[:500],
                    "receipt_json": _json_dumps(receipt) if receipt else None,
                    "tools_json": _json_dumps(tools) if tools is not None else None,
                    "error": error[:500],
                    "terminal": terminal,
                },
            )
        return
    _ensure_sqlite_schema()
    with store._LOCK, store._connect() as conn:
        conn.execute(
            """
            UPDATE openclaw_action_runs
            SET status=?,
                result_summary=CASE WHEN ? <> '' THEN ? ELSE result_summary END,
                receipt_json=CASE WHEN ? <> '' THEN ? ELSE receipt_json END,
                tools_json=CASE WHEN ? <> '' THEN ? ELSE tools_json END,
                error=?,
                started_at=CASE
                    WHEN started_at IS NULL AND ? IN ('planning', 'running')
                    THEN ? ELSE started_at END,
                finished_at=CASE WHEN ? THEN ? ELSE finished_at END,
                updated_at=?
            WHERE org_id=? AND run_id=? AND action_id=?
            """,
            (
                state, summary[:500], summary[:500],
                _json_dumps(receipt) if receipt else "",
                _json_dumps(receipt) if receipt else "",
                _json_dumps(tools) if tools is not None else "",
                _json_dumps(tools) if tools is not None else "",
                error[:500], state, now, 1 if terminal else 0, now, now,
                org_id, run_id, action_id,
            ),
        )


def _action_ids_for_run(org_id: str, run_id: str) -> list[str]:
    detail = run_detail(org_id, run_id) or {}
    return [str(a.get("action_id") or "") for a in detail.get("actions") or []]


def _auto_approve_and_claim(org_id: str, run_id: str, actions: list[dict]) -> None:
    for action in actions:
        aid = str(action.get("action_id") or "")
        if not aid:
            continue
        ledger.set_action_route(aid, "openclaw", org_id=org_id)
        typed = action.get("typed") if isinstance(action.get("typed"), dict) else {}
        missing = action_plane.missing_params(typed)
        if missing:
            ledger.set_action_status(
                aid,
                "needs_details",
                "OpenClaw needs details: " + ", ".join(missing)[:240],
                org_id=org_id,
            )
            _set_action_run(
                org_id, run_id, aid, "needs_attention",
                summary="Missing required action parameters",
                error="missing_params:" + ",".join(missing),
            )
            _event(
                org_id, run_id, "action_needs_attention",
                "Missing required action parameters",
                action_id=aid, status="needs_attention",
                safe={"missing_params": missing},
            )
            continue
        idem = action_plane.execution_idempotency_key(aid)
        ledger.record_action_decision(
            aid, org_id=org_id, decision="approve", idempotency_key=idem,
            decided_via="openclaw", laura_user_id="system:openclaw-test",
            new_status="approved",
        )
        ledger.set_action_status(
            aid, "approved", "approved via OpenClaw experiment", org_id=org_id,
        )
        if ledger.claim_action_execution(
            aid, org_id=org_id, idempotency_key=idem, via="openclaw"
        ):
            _set_action_run(org_id, run_id, aid, "running",
                            summary="Execution claim acquired")
            _event(org_id, run_id, "action_claimed",
                   "Execution claim acquired", action_id=aid, status="running")
        else:
            latest = (ledger.action_statuses([aid], org_id=org_id).get(aid) or {})
            _set_action_run(
                org_id, run_id, aid, "needs_attention",
                summary="Canonical action was already claimed or settled",
                error="claim_lost",
            )
            _event(
                org_id, run_id, "action_claim_lost",
                "Canonical action was already claimed or settled",
                action_id=aid,
                status=str(latest.get("status") or "unknown"),
            )


def create_meeting_run(bot_id: str, artifact: dict, org_id: str,
                       *, auto_start: bool = True) -> dict:
    """Create the one OpenClaw run for a finalized meeting, idempotently."""
    org = str(org_id or "").strip()
    meeting_id = str(bot_id or "").strip()
    if not gates.experiment_enabled_for_org(org):
        return {"ok": False, "skipped": "openclaw disabled for org"}
    if not meeting_id:
        return {"ok": False, "error": "missing meeting_id"}
    payload = _run_input(meeting_id, artifact or {}, org)
    run, created = _insert_run(org, meeting_id, payload)
    run_id = str(run.get("run_id") or "")
    if not run_id:
        return {"ok": False, "error": "could not create run"}
    if not created:
        return {
            "ok": True,
            "run": run_detail(org, run_id) or run,
            "replay": True,
        }
    _insert_action_runs(org, run_id, payload["actions"])
    _event(
        org, run_id, "run_created", "OpenClaw run created",
        safe={
            "meeting_id": meeting_id,
            "actions": len(payload["actions"]),
            "auto_run": bool(settings.openclaw_auto_run),
        },
    )
    if not payload["actions"]:
        _update_run(org, run_id, "done", metrics={"actions": 0})
        _event(org, run_id, "run_done", "No actions to execute", status="done")
    elif settings.openclaw_auto_run and auto_start:
        _auto_approve_and_claim(org, run_id, payload["actions"])
        start_run_async(org, run_id)
    else:
        _event(
            org, run_id, "auto_run_disabled",
            "OpenClaw auto-run is disabled; run is queued",
            status="queued",
        )
    shaped = run_detail(org, run_id) or run
    return {"ok": True, "run": shaped}


def start_run_async(org_id: str, run_id: str) -> None:
    thread = threading.Thread(
        target=lambda: run_openclaw(org_id, run_id), daemon=True
    )
    thread.start()


def _cap_secret() -> str:
    return (
        (settings.session_secret or "").strip()
        or (settings.laura_webhook_secret or "").strip()
        or (settings.laura_api_token or "").strip()
        or "dev-openclaw-capability"
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("ascii"))


def mint_capability(org_id: str, run_id: str, *, ttl_seconds: int = 3600) -> str:
    payload = {
        "org_id": str(org_id or ""),
        "run_id": str(run_id or ""),
        "exp": int(_now() + max(60, min(int(ttl_seconds or 3600), 24 * 3600))),
        "tools": list(ALL_TOOLS),
    }
    body = _b64(_json_dumps(payload).encode("utf-8"))
    sig = hmac.new(_cap_secret().encode("utf-8"), body.encode("ascii"),
                   hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def verify_capability(token: str) -> dict | None:
    raw = str(token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    body, sep, sig = raw.partition(".")
    if not (body and sep and sig):
        return None
    expected = _b64(
        hmac.new(_cap_secret().encode("utf-8"), body.encode("ascii"),
                 hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if int(payload.get("exp") or 0) < int(_now()):
        return None
    org = str(payload.get("org_id") or "")
    run_id = str(payload.get("run_id") or "")
    if not get_run(org, run_id):
        return None
    return payload


def _tool_for_action_type(action_type: str) -> str:
    typed = str(action_type or "").strip()
    if pipedream_executor.generic_app(typed):
        return "pipedream_run_app_action"
    if typed in pipedream_executor.action_types() and not native_runtime.supports(
        typed
    ):
        return "pipedream_run_app_action"
    return _ACTION_TOOL_BY_TYPE.get(typed, "")


def _action_for_run(org_id: str, run_id: str, action_id: str) -> dict | None:
    run = get_run(org_id, run_id) or {}
    payload = run.get("input") if isinstance(run.get("input"), dict) else {}
    for action in payload.get("actions") or []:
        if (
            isinstance(action, dict)
            and str(action.get("action_id") or "") == action_id
        ):
            return action
    return None


def _openresponses_tools(run: dict) -> list[dict]:
    payload = run.get("input") if isinstance(run.get("input"), dict) else {}
    ids_by_tool: dict[str, list[str]] = {}
    for action in payload.get("actions") or []:
        if not isinstance(action, dict) or action.get("missing_params"):
            continue
        typed = action.get("typed") if isinstance(action.get("typed"), dict) else {}
        tool = _tool_for_action_type(str(typed.get("type") or ""))
        aid = str(action.get("action_id") or "")
        if tool and aid:
            ids_by_tool.setdefault(tool, []).append(aid)

    descriptions = {
        "gmail_send": "Send the canonical approved email action.",
        "calendar_create_event": "Create the canonical approved calendar event.",
        "asana_create_task": "Create the canonical approved Asana task.",
        "asana_update_task": "Update the canonical approved Asana task.",
        "asana_add_comment": "Add the canonical approved Asana comment.",
        "pipedream_run_app_action": (
            "Run the canonical approved connected-app action through Pipedream."
        ),
        "browser_fallback": "Request manual browser attention for this action.",
    }
    tools: list[dict] = []
    for tool, action_ids in sorted(ids_by_tool.items()):
        tools.append(
            {
                "type": "function",
                "name": tool,
                "description": descriptions[tool],
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action_id": {
                            "type": "string",
                            "enum": sorted(set(action_ids)),
                            "description": "Canonical action ID from the plan.",
                        },
                        "step_id": {
                            "type": "string",
                            "description": (
                                "Stable idempotency step ID for this one action."
                            ),
                        },
                    },
                    "required": ["action_id", "step_id"],
                    "additionalProperties": False,
                },
            }
        )
    return tools


def _openresponses_calls(data: dict) -> list[dict]:
    calls: list[dict] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        arguments = item.get("arguments")
        if isinstance(arguments, str):
            arguments = _json_loads(arguments, {})
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(
            {
                "call_id": str(item.get("call_id") or item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "arguments": arguments,
            }
        )
    return calls


def _settle_gateway_failure(
    org_id: str,
    run_id: str,
    error: str,
    *,
    run_status: str = "failed",
    code: str = "gateway_failed",
) -> None:
    detail = run_detail(org_id, run_id) or {}
    for action in detail.get("actions") or []:
        aid = str(action.get("action_id") or "")
        if not aid or str(action.get("status") or "") in TERMINAL_RUN_STATUSES:
            continue
        _set_action_run(
            org_id,
            run_id,
            aid,
            "needs_attention" if run_status == "needs_attention" else "failed",
            summary="OpenClaw execution did not complete",
            error=code,
        )
        ledger.set_action_status(
            aid,
            "failed",
            error,
            org_id=org_id,
            receipt={
                "kind": "OpenClaw",
                "route": "openclaw",
                "error": code,
            },
        )
    _update_run(org_id, run_id, run_status, error=error)
    _event(
        org_id,
        run_id,
        run_status,
        error,
        status=run_status,
        safe={"error_code": code},
    )


def _finish_openresponses_run(
    org_id: str,
    run_id: str,
    *,
    responses: int,
    tool_calls: int,
) -> None:
    detail = run_detail(org_id, run_id) or {}
    statuses = [
        str(action.get("status") or "")
        for action in detail.get("actions") or []
    ]
    metrics = {"responses": responses, "tool_calls": tool_calls}
    if statuses and all(status == "done" for status in statuses):
        _update_run(org_id, run_id, "done", metrics=metrics)
        _event(
            org_id,
            run_id,
            "run_done",
            "OpenClaw completed all actions",
            status="done",
            safe=metrics,
        )
        return
    reason = (
        "OpenClaw finished without completing every canonical action; "
        "no legacy fallback ran."
    )
    _update_run(
        org_id,
        run_id,
        "needs_attention",
        metrics=metrics,
        error=reason,
    )
    _event(
        org_id,
        run_id,
        "needs_attention",
        reason,
        status="needs_attention",
        safe=metrics,
    )


def run_openclaw(org_id: str, run_id: str) -> None:
    """Execute a finalized meeting through OpenClaw's OpenResponses API."""
    run = get_run(org_id, run_id)
    if not run or run.get("status") == "cancelled":
        return
    _update_run(org_id, run_id, "planning")
    _event(
        org_id,
        run_id,
        "planning",
        "Preparing OpenClaw execution plan",
        status="planning",
    )
    gateway = (settings.openclaw_gateway_url or "").strip().rstrip("/")
    if not gateway:
        _settle_gateway_failure(
            org_id,
            run_id,
            "OpenClaw gateway is not configured; no legacy fallback ran.",
            run_status="needs_attention",
            code="gateway_not_configured",
        )
        return

    tools = _openresponses_tools(run)
    if not tools:
        _settle_gateway_failure(
            org_id,
            run_id,
            "OpenClaw found no executable canonical tools; no fallback ran.",
            run_status="needs_attention",
            code="no_executable_tools",
        )
        return

    _update_run(org_id, run_id, "running")
    capability = mint_capability(org_id, run_id)
    response_url = (
        gateway
        if gateway.endswith("/v1/responses")
        else f"{gateway}/v1/responses"
    )
    headers = {
        "Content-Type": "application/json",
        "x-openclaw-session-key": f"laura-openclaw-{run_id}",
    }
    agent_id = str(settings.openclaw_agent_id or "main").strip()
    if agent_id:
        headers["x-openclaw-agent-id"] = agent_id
    if settings.openclaw_gateway_token:
        headers["Authorization"] = f"Bearer {settings.openclaw_gateway_token}"

    instructions = (
        "You are Laura's post-meeting action executor. The JSON plan contains "
        "actions that Laura has already approved and claimed. Execute every "
        "action exactly once using its matching client function tool. Pass the "
        "canonical action_id unchanged and choose one stable step_id. Laura "
        "will load the authoritative stored arguments, so never invent or "
        "modify recipients, times, task fields, or app parameters. Do not call "
        "a side-effect tool twice for the same action. When all actions have "
        "tool results, respond with a concise completion summary."
    )
    request: dict = {
        "model": "openclaw",
        "instructions": instructions,
        "input": (
            "Execute this finalized meeting action plan:\n"
            + _json_dumps(run.get("input") or {})
        ),
        "tools": tools,
        "tool_choice": "required",
        "stream": False,
        "user": f"laura-openclaw-{run_id}",
        "max_output_tokens": 1200,
    }

    responses = 0
    tool_calls = 0
    gateway_run_id = ""
    for _turn in range(_MAX_OPENRESPONSES_TURNS):
        current = get_run(org_id, run_id) or {}
        if current.get("status") == "cancelled":
            return
        try:
            import httpx

            resp = httpx.post(
                response_url,
                json=request,
                headers=headers,
                timeout=90,
            )
            data = (
                resp.json()
                if resp.headers.get("content-type", "").startswith(
                    "application/json"
                )
                else {}
            )
        except Exception as exc:  # noqa: BLE001
            _settle_gateway_failure(
                org_id,
                run_id,
                f"OpenClaw gateway request failed ({type(exc).__name__})",
                code="gateway_request_failed",
            )
            return
        if resp.status_code >= 400:
            _settle_gateway_failure(
                org_id,
                run_id,
                f"OpenClaw gateway returned HTTP {resp.status_code}",
                code=f"gateway_http_{resp.status_code}",
            )
            return

        responses += 1
        response_id = str(data.get("id") or "")
        if not gateway_run_id:
            gateway_run_id = response_id
            _update_run(
                org_id,
                run_id,
                "running",
                gateway_run_id=gateway_run_id,
            )
            _event(
                org_id,
                run_id,
                "gateway_started",
                "OpenClaw accepted the run",
                status="running",
                safe={"gateway_run_id": gateway_run_id},
            )

        calls = _openresponses_calls(data)
        if not calls:
            _finish_openresponses_run(
                org_id,
                run_id,
                responses=responses,
                tool_calls=tool_calls,
            )
            return
        if not response_id:
            _settle_gateway_failure(
                org_id,
                run_id,
                "OpenClaw returned tool calls without a response ID.",
                code="missing_response_id",
            )
            return

        outputs: list[dict] = []
        for call in calls:
            call_id = call["call_id"]
            if not call_id:
                _settle_gateway_failure(
                    org_id,
                    run_id,
                    "OpenClaw returned a tool call without a call ID.",
                    code="missing_call_id",
                )
                return
            result = run_tool(
                f"Bearer {capability}",
                call["name"],
                call["arguments"],
            )
            tool_calls += 1
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _json_dumps(result),
                }
            )

        detail = run_detail(org_id, run_id) or {}
        unfinished = any(
            str(action.get("status") or "")
            not in TERMINAL_RUN_STATUSES
            for action in detail.get("actions") or []
        )
        request = {
            "model": "openclaw",
            "instructions": instructions,
            "input": outputs,
            "previous_response_id": response_id,
            "tools": tools,
            "tool_choice": "required" if unfinished else "none",
            "stream": False,
            "user": f"laura-openclaw-{run_id}",
            "max_output_tokens": 1200,
        }

    _settle_gateway_failure(
        org_id,
        run_id,
        "OpenClaw exceeded the bounded tool-call loop.",
        code="turn_limit_exceeded",
    )


def _tool_existing(org_id: str, run_id: str, action_id: str,
                   step_id: str) -> dict | None:
    if _pg(org_id):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            row = _pg_fetchone(
                conn,
                """
                SELECT * FROM openclaw_tool_calls
                WHERE org_id=:org_id AND run_id=:run_id
                  AND action_id=:action_id AND step_id=:step_id
                """,
                {
                    "org_id": org_id, "run_id": run_id,
                    "action_id": action_id, "step_id": step_id,
                },
            )
        return row
    _ensure_sqlite_schema()
    with store._LOCK, store._connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM openclaw_tool_calls
            WHERE org_id=? AND run_id=? AND action_id=? AND step_id=?
            """,
            (org_id, run_id, action_id, step_id),
        ).fetchone()
    return dict(row) if row else None


def _tool_existing_for_action(
    org_id: str, run_id: str, action_id: str
) -> dict | None:
    if _pg(org_id):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            row = _pg_fetchone(
                conn,
                """
                SELECT * FROM openclaw_tool_calls
                WHERE org_id=:org_id AND run_id=:run_id
                  AND action_id=:action_id
                ORDER BY created_at ASC
                LIMIT 1
                """,
                {
                    "org_id": org_id,
                    "run_id": run_id,
                    "action_id": action_id,
                },
            )
        return row
    _ensure_sqlite_schema()
    with store._LOCK, store._connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM openclaw_tool_calls
            WHERE org_id=? AND run_id=? AND action_id=?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (org_id, run_id, action_id),
        ).fetchone()
    return dict(row) if row else None


def _insert_tool_call(org_id: str, run_id: str, action_id: str, step_id: str,
                      tool: str, request: dict) -> bool:
    now = _now()
    idem = str(request.get("idempotency_key") or "")[:160]
    if _pg(org_id):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            inserted = conn.execute(
                _text(
                    """
                    INSERT INTO openclaw_tool_calls
                      (org_id, run_id, action_id, step_id, idempotency_key, tool,
                       status, request_json, result_json, created_at, updated_at)
                    VALUES
                      (:org_id, :run_id, :action_id, :step_id, :idempotency_key,
                       :tool, 'running', CAST(:request_json AS jsonb),
                       '{}'::jsonb, clock_timestamp(), clock_timestamp())
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "org_id": org_id, "run_id": run_id, "action_id": action_id,
                    "step_id": step_id, "idempotency_key": idem, "tool": tool,
                    "request_json": _json_dumps(request),
                },
            )
        return inserted.rowcount == 1
    _ensure_sqlite_schema()
    with store._LOCK, store._connect() as conn:
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO openclaw_tool_calls
              (org_id, run_id, action_id, step_id, idempotency_key, tool, status,
               request_json, result_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?, '{}', ?, ?)
            """,
            (
                org_id, run_id, action_id, step_id, idem, tool,
                _json_dumps(request), now, now,
            ),
        )
    return inserted.rowcount == 1


def _finish_tool_call(org_id: str, run_id: str, action_id: str, step_id: str,
                      status: str, result: dict, error: str = "") -> None:
    now = _now()
    if _pg(org_id):
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            conn.execute(
                _text(
                    """
                    UPDATE openclaw_tool_calls
                    SET status=:status,
                        result_json=CAST(:result_json AS jsonb),
                        error=:error,
                        updated_at=clock_timestamp()
                    WHERE org_id=:org_id AND run_id=:run_id
                      AND action_id=:action_id AND step_id=:step_id
                    """
                ),
                {
                    "org_id": org_id, "run_id": run_id, "action_id": action_id,
                    "step_id": step_id, "status": status,
                    "result_json": _json_dumps(result), "error": error[:500],
                },
            )
        return
    _ensure_sqlite_schema()
    with store._LOCK, store._connect() as conn:
        conn.execute(
            """
            UPDATE openclaw_tool_calls
            SET status=?, result_json=?, error=?, updated_at=?
            WHERE org_id=? AND run_id=? AND action_id=? AND step_id=?
            """,
            (
                status, _json_dumps(result), error[:500], now,
                org_id, run_id, action_id, step_id,
            ),
        )


def _shape_tool_row(row: dict) -> dict:
    return {
        "ok": str(row.get("status") or "") == "done",
        "status": str(row.get("status") or ""),
        "result": _json_loads(row.get("result_json"), {}),
        "error": str(row.get("error") or ""),
        "replay": True,
    }


def _tool_action(tool_name: str, args: dict) -> dict | None:
    if tool_name == "gmail_send":
        return {"type": "email.send", "message": dict(args)}
    if tool_name == "calendar_create_event":
        return {"type": "calendar.create_event", "event": dict(args)}
    if tool_name == "asana_create_task":
        return {"type": "asana.create_task", "task": dict(args)}
    if tool_name == "asana_update_task":
        return {"type": "asana.update_task", "task": dict(args)}
    if tool_name == "asana_add_comment":
        return {"type": "asana.add_comment", "task": dict(args)}
    if tool_name == "pipedream_run_app_action":
        app = str(args.get("app") or "").strip()
        action_key = str(args.get("action_key") or "").strip()
        props = args.get("props") if isinstance(args.get("props"), dict) else {}
        if app and action_key:
            return {
                "type": f"pd.{app}.run",
                "args": {"action_key": action_key, "props": props},
            }
    return None


def _execute_side_effect(
    org_id: str,
    action_id: str,
    tool_name: str,
    action_type: str,
    args: dict,
) -> dict:
    if tool_name == "browser_fallback":
        if not settings.openclaw_browser_enabled:
            return {"ok": False, "error": "browser fallback is disabled"}
        return {
            "ok": False,
            "status": "needs_attention",
            "error": "browser fallback requires a manual authenticated browser session",
            "kind": "browser fallback",
            "route": "openclaw",
        }
    if not action_type:
        return {"ok": False, "error": f"unsupported OpenClaw tool {tool_name!r}"}
    action = {"type": action_type, "args": dict(args)}
    if pipedream_executor.generic_app(action_type) or (
        pipedream_executor.handles(action)
        and not native_runtime.supports(action_type)
    ):
        return pipedream_executor.execute_for_openclaw(org_id, action_id, action)
    if native_runtime.supports(action_type):
        return executor.execute_for_openclaw(org_id, action_id, action)
    if pipedream_executor.handles(action):
        return pipedream_executor.execute_for_openclaw(org_id, action_id, action)
    return {"ok": False, "error": f"no connected executor for {action_type!r}"}


def run_tool(capability_token: str, tool_name: str, request: dict) -> dict:
    cap = verify_capability(capability_token)
    if cap is None:
        return {"ok": False, "status": 401, "error": "invalid capability"}
    tool = str(tool_name or "").strip()
    if tool not in ALL_TOOLS:
        return {"ok": False, "status": 404, "error": "unknown tool"}
    org_id = str(cap.get("org_id") or "")
    run_id = str(cap.get("run_id") or "")
    if tool == "laura_connected_tools":
        return {"ok": True, "tools": tool_catalog(org_id)}
    if tool == "pipedream_list_accounts":
        app = str((request or {}).get("app") or "").strip()
        try:
            return {"ok": True, "accounts": pipedream_client.list_accounts(org_id, app=app)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"pipedream unavailable ({type(exc).__name__})"}
    if tool == "pipedream_list_app_actions":
        query = str((request or {}).get("query") or "")
        app = str((request or {}).get("app") or query or "")
        try:
            return {"ok": True, "actions": pipedream_client.list_actions(app or query, limit=30)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"pipedream unavailable ({type(exc).__name__})"}

    body = request if isinstance(request, dict) else {}
    action_id = str(body.get("action_id") or "").strip()
    step_id = str(body.get("step_id") or body.get("idempotency_key") or "").strip()
    if not (action_id and step_id):
        return {
            "ok": False,
            "status": 400,
            "error": "side-effect tools require action_id and step_id",
        }
    canonical = _action_for_run(org_id, run_id, action_id)
    if canonical is None:
        return {
            "ok": False,
            "status": 403,
            "error": "action does not belong to this OpenClaw run",
        }
    typed = (
        canonical.get("typed")
        if isinstance(canonical.get("typed"), dict)
        else {}
    )
    action_type = str(typed.get("type") or "").strip()
    expected_tool = _tool_for_action_type(action_type)
    if not expected_tool or tool != expected_tool:
        return {
            "ok": False,
            "status": 403,
            "error": "tool does not match the canonical action type",
        }
    canonical_args = (
        typed.get("args") if isinstance(typed.get("args"), dict) else {}
    )
    existing = _tool_existing_for_action(org_id, run_id, action_id)
    if existing is not None:
        return _shape_tool_row(existing)
    inserted = _insert_tool_call(
        org_id, run_id, action_id, step_id, tool, body
    )
    if not inserted:
        existing = _tool_existing_for_action(org_id, run_id, action_id)
        return _shape_tool_row(existing or {})
    _event(org_id, run_id, "tool_started", "OpenClaw tool call started",
           action_id=action_id, tool=tool, status="running")
    result = _execute_side_effect(
        org_id,
        action_id,
        tool,
        action_type,
        dict(canonical_args),
    )
    ok = bool(result.get("ok"))
    status = "done" if ok else str(result.get("status") or "failed")
    if status not in ACTION_RUN_STATUSES:
        status = "failed"
    error = "" if ok else str(result.get("error") or result.get("skipped") or "failed")
    _finish_tool_call(org_id, run_id, action_id, step_id,
                      "done" if ok else "failed", result, error=error)
    _set_action_run(
        org_id, run_id, action_id, status,
        summary=str(result.get("kind") or tool),
        receipt=result if ok else {},
        tools=[{"tool": tool, "step_id": step_id, "ok": ok}],
        error=error,
    )
    _event(
        org_id, run_id, "tool_done" if ok else "tool_failed",
        str(result.get("kind") or error or tool)[:300],
        action_id=action_id, tool=tool, status=status,
        safe={"ok": ok, "kind": result.get("kind"), "route": result.get("route")},
    )
    return {"ok": ok, "status": status, "result": result, "replay": False}


def record_gateway_event(capability_token: str, body: dict) -> dict:
    cap = verify_capability(capability_token)
    if cap is None:
        return {"ok": False, "status": 401, "error": "invalid capability"}
    org_id = str(cap.get("org_id") or "")
    run_id = str(cap.get("run_id") or "")
    data = body if isinstance(body, dict) else {}
    action_id = str(data.get("action_id") or "")
    status = str(data.get("status") or "").strip().lower()
    kind = str(data.get("kind") or "gateway_event")[:120]
    summary = str(data.get("summary") or "")[:500]
    safe = data.get("safe") if isinstance(data.get("safe"), dict) else {}
    if status in RUN_STATUSES and not action_id:
        _update_run(org_id, run_id, status, error=str(data.get("error") or ""))
    if status in ACTION_RUN_STATUSES and action_id:
        receipt = data.get("receipt") if isinstance(data.get("receipt"), dict) else {}
        tools = data.get("tools") if isinstance(data.get("tools"), list) else None
        _set_action_run(
            org_id, run_id, action_id, status, summary=summary,
            receipt=receipt, tools=tools, error=str(data.get("error") or ""),
        )
        if status == "done":
            ledger.set_action_status(
                action_id, "done", summary or "OpenClaw completed action",
                org_id=org_id, receipt={"route": "openclaw", **receipt},
            )
        elif status == "failed":
            ledger.set_action_status(
                action_id, "failed", summary or "OpenClaw failed action",
                org_id=org_id, receipt={"route": "openclaw", **receipt},
            )
        elif status == "needs_attention":
            ledger.set_action_status(
                action_id, "needs_details",
                summary or "OpenClaw needs attention", org_id=org_id,
            )
    _event(
        org_id, run_id, kind, summary, action_id=action_id,
        tool=str(data.get("tool") or ""), status=status, safe=safe,
    )
    return {"ok": True}


def cancel_run(org_id: str, run_id: str, *, reason: str = "cancelled") -> bool:
    if not get_run(org_id, run_id):
        return False
    _update_run(org_id, run_id, "cancelled", error=reason[:500])
    for aid in _action_ids_for_run(org_id, run_id):
        _set_action_run(
            org_id, run_id, aid, "cancelled",
            summary="OpenClaw run cancelled", error=reason[:500],
        )
        ledger.set_action_status(
            aid, "failed", "OpenClaw run cancelled", org_id=org_id,
            receipt={"kind": "OpenClaw", "route": "openclaw", "error": "cancelled"},
        )
    _event(org_id, run_id, "cancelled", reason[:500], status="cancelled")
    return True


def replay_meeting(org_id: str, meeting_id: str) -> dict:
    artifact = store.get_artifact(meeting_id, org_id)
    if artifact is None:
        return {"ok": False, "error": "unknown meeting for this org"}
    return create_meeting_run(meeting_id, artifact, org_id, auto_start=True)
