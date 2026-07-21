"""Postgres DAL for org avatar overlays (M2) — outbox_pg discipline.

Draft → publish → history is strictly linear and append-only:

- exactly ONE editable draft per (org, avatar) — enforced by a partial unique
  index; edits converge on it under an optimistic ``version_token``;
- publish freezes the draft row forever (``published``) and moves the single
  ``org_avatars.current_version`` pointer inside one FOR UPDATE transaction,
  so two concurrent publishes serialize and the loser gets a clean conflict;
- rollback republishes an OLD version's payload as a NEW version — history is
  never rewritten, and provenance always answers "what was live at time T".

Every write appends to ``org_avatar_audit`` (INSERT-only by grant). Every
function pins ``app.current_org`` first — FORCE RLS is the backstop. Nothing
here logs overlay content beyond field names.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text

from . import control_plane
from ..config import settings


def _engine():
    return control_plane._get_engine()


def _set_org(conn, org_id: str) -> None:
    control_plane._set_org(conn, org_id)


def enabled() -> bool:
    """The M2 master switch: the feature flag AND a control plane. Off ⇒
    every runtime path is byte-identical to canonical behavior."""
    return bool(settings.org_avatar_overlays_enabled) and control_plane.enabled()


def _audit(conn, org_id: str, avatar_key: str, action: str, actor: str,
           detail: dict | None = None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO org_avatar_audit
              (org_id, avatar_key, action, actor, detail_json)
            VALUES (:org_id, :avatar_key, :action, :actor,
                    CAST(:detail AS jsonb))
            """
        ),
        {
            "org_id": org_id,
            "avatar_key": avatar_key[:64],
            "action": action[:40],
            "actor": str(actor or "")[:64],
            "detail": json.dumps(detail or {}, separators=(",", ":"),
                                 sort_keys=True)[:2000],
        },
    )


def _overlay_json(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def ensure_org_avatar(org_id: str, avatar_key: str, actor: str) -> dict[str, Any]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                """
                INSERT INTO org_avatars (org_id, avatar_key, created_by,
                                         updated_by)
                VALUES (:org_id, :avatar_key, :actor, :actor)
                ON CONFLICT (org_id, avatar_key) DO NOTHING
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key, "actor": actor[:64]},
        )
        row = conn.execute(
            text(
                "SELECT avatar_key, enabled, current_version, "
                "extract(epoch from updated_at) AS updated_at "
                "FROM org_avatars "
                "WHERE org_id=:org_id AND avatar_key=:avatar_key"
            ),
            {"org_id": org_id, "avatar_key": avatar_key},
        ).mappings().one()
    return dict(row)


def list_org_avatars(org_id: str) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT a.avatar_key, a.enabled, a.current_version,
                       extract(epoch from a.updated_at) AS updated_at,
                       (SELECT COUNT(*) FROM org_avatar_versions v
                         WHERE v.org_id=a.org_id
                           AND v.avatar_key=a.avatar_key
                           AND v.status='draft') AS has_draft,
                       (SELECT COUNT(*) FROM org_avatar_assignments s
                         WHERE s.org_id=a.org_id
                           AND s.avatar_key=a.avatar_key
                           AND s.active) AS assignments
                FROM org_avatars a
                WHERE a.org_id=:org_id
                ORDER BY a.avatar_key
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def get_draft(org_id: str, avatar_key: str) -> Optional[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT version, overlay_json, change_note,
                       version_token::text, created_by,
                       extract(epoch from created_at) AS created_at
                FROM org_avatar_versions
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                  AND status='draft'
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key},
        ).mappings().first()
    if row is None:
        return None
    out = dict(row)
    out["overlay_json"] = _overlay_json(out["overlay_json"])
    return out


def list_versions(org_id: str, avatar_key: str) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT version, status, change_note, created_by, published_by,
                       extract(epoch from created_at) AS created_at,
                       extract(epoch from published_at) AS published_at
                FROM org_avatar_versions
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                ORDER BY version DESC
                LIMIT 50
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key},
        ).mappings().all()
    return [dict(r) for r in rows]


def get_version(
    org_id: str, avatar_key: str, version: int
) -> Optional[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT version, status, overlay_json, change_note,
                       created_by, published_by,
                       extract(epoch from published_at) AS published_at
                FROM org_avatar_versions
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                  AND version=:version
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key,
             "version": int(version)},
        ).mappings().first()
    if row is None:
        return None
    out = dict(row)
    out["overlay_json"] = _overlay_json(out["overlay_json"])
    return out


def save_draft(
    org_id: str, avatar_key: str, overlay: dict, actor: str,
    *, expected_token: str = "", change_note: str = "",
) -> tuple[Optional[dict[str, Any]], str]:
    """Create or update THE draft. Returns (draft_row, error).

    Optimistic concurrency: when a draft exists, the caller must present its
    current ``version_token`` — a stale editor gets 'version_conflict', never
    a silent overwrite. Every successful save rotates the token."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                "SELECT 1 FROM org_avatars "
                "WHERE org_id=:org_id AND avatar_key=:avatar_key FOR UPDATE"
            ),
            {"org_id": org_id, "avatar_key": avatar_key},
        )
        draft = conn.execute(
            text(
                """
                SELECT version, version_token::text
                FROM org_avatar_versions
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                  AND status='draft'
                FOR UPDATE
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key},
        ).mappings().first()
        payload = json.dumps(overlay, separators=(",", ":"), sort_keys=True)
        if draft is not None:
            if expected_token and expected_token != draft["version_token"]:
                return None, "version_conflict"
            conn.execute(
                text(
                    """
                    UPDATE org_avatar_versions
                    SET overlay_json=CAST(:payload AS jsonb),
                        change_note=:note,
                        version_token=gen_random_uuid(),
                        created_by=:actor
                    WHERE org_id=:org_id AND avatar_key=:avatar_key
                      AND version=:version
                    """
                ),
                {"org_id": org_id, "avatar_key": avatar_key,
                 "version": draft["version"], "payload": payload,
                 "note": change_note[:300], "actor": actor[:64]},
            )
            version = int(draft["version"])
        else:
            row = conn.execute(
                text(
                    """
                    INSERT INTO org_avatar_versions (
                      org_id, avatar_key, version, status, overlay_json,
                      change_note, created_by
                    )
                    SELECT :org_id, :avatar_key,
                           COALESCE(MAX(version), 0) + 1, 'draft',
                           CAST(:payload AS jsonb), :note, :actor
                    FROM org_avatar_versions
                    WHERE org_id=:org_id AND avatar_key=:avatar_key
                    RETURNING version
                    """
                ),
                {"org_id": org_id, "avatar_key": avatar_key,
                 "payload": payload, "note": change_note[:300],
                 "actor": actor[:64]},
            ).first()
            version = int(row[0])
        _audit(conn, org_id, avatar_key, "draft_saved", actor,
               {"version": version, "fields": sorted(overlay)})
        fresh = conn.execute(
            text(
                """
                SELECT version, overlay_json, change_note,
                       version_token::text,
                       extract(epoch from created_at) AS created_at
                FROM org_avatar_versions
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                  AND version=:version
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key, "version": version},
        ).mappings().one()
    out = dict(fresh)
    out["overlay_json"] = _overlay_json(out["overlay_json"])
    return out, ""


def publish_draft(
    org_id: str, avatar_key: str, actor: str, *, expected_token: str = "",
) -> tuple[int, str]:
    """Atomically publish THE draft. Returns (published_version, error).

    FOR UPDATE on the org_avatars row serializes concurrent publishes: the
    winner freezes the draft and moves the pointer; the loser finds no draft
    (or a rotated token) and gets a clean conflict — the pointer can never be
    corrupted or moved twice."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        head = conn.execute(
            text(
                "SELECT current_version FROM org_avatars "
                "WHERE org_id=:org_id AND avatar_key=:avatar_key FOR UPDATE"
            ),
            {"org_id": org_id, "avatar_key": avatar_key},
        ).first()
        if head is None:
            return 0, "unknown_avatar"
        draft = conn.execute(
            text(
                """
                SELECT version, version_token::text
                FROM org_avatar_versions
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                  AND status='draft'
                FOR UPDATE
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key},
        ).mappings().first()
        if draft is None:
            return 0, "no_draft"
        if expected_token and expected_token != draft["version_token"]:
            return 0, "version_conflict"
        conn.execute(
            text(
                """
                UPDATE org_avatar_versions
                SET status='published', published_by=:actor,
                    published_at=clock_timestamp()
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                  AND version=:version
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key,
             "version": draft["version"], "actor": actor[:64]},
        )
        conn.execute(
            text(
                """
                UPDATE org_avatars
                SET current_version=:version, updated_by=:actor,
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key,
             "version": draft["version"], "actor": actor[:64]},
        )
        _audit(conn, org_id, avatar_key, "published", actor,
               {"version": int(draft["version"])})
    return int(draft["version"]), ""


def publish_prior(
    org_id: str, avatar_key: str, version: int, actor: str
) -> tuple[int, str]:
    """Rollback = republish an older PUBLISHED/ARCHIVED version's payload as a
    brand-new published version (traceable, append-only). Returns
    (new_version, error)."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        head = conn.execute(
            text(
                "SELECT current_version FROM org_avatars "
                "WHERE org_id=:org_id AND avatar_key=:avatar_key FOR UPDATE"
            ),
            {"org_id": org_id, "avatar_key": avatar_key},
        ).first()
        if head is None:
            return 0, "unknown_avatar"
        source = conn.execute(
            text(
                """
                SELECT overlay_json, status FROM org_avatar_versions
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                  AND version=:version
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key,
             "version": int(version)},
        ).mappings().first()
        if source is None:
            return 0, "unknown_version"
        if source["status"] == "draft":
            return 0, "cannot_rollback_to_draft"
        row = conn.execute(
            text(
                """
                INSERT INTO org_avatar_versions (
                  org_id, avatar_key, version, status, overlay_json,
                  change_note, created_by, published_by, published_at
                )
                SELECT :org_id, :avatar_key, COALESCE(MAX(version), 0) + 1,
                       'published', CAST(:payload AS jsonb), :note,
                       :actor, :actor, clock_timestamp()
                FROM org_avatar_versions
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                RETURNING version
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key,
             "payload": json.dumps(
                 _overlay_json(source["overlay_json"]),
                 separators=(",", ":"), sort_keys=True),
             "note": f"rollback: republish of v{int(version)}",
             "actor": actor[:64]},
        ).first()
        new_version = int(row[0])
        conn.execute(
            text(
                """
                UPDATE org_avatars
                SET current_version=:version, updated_by=:actor,
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key,
             "version": new_version, "actor": actor[:64]},
        )
        _audit(conn, org_id, avatar_key, "rollback", actor,
               {"from_version": int(version), "new_version": new_version})
    return new_version, ""


def set_enabled(org_id: str, avatar_key: str, enabled_: bool, actor: str) -> bool:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                """
                UPDATE org_avatars
                SET enabled=:enabled, updated_by=:actor,
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND avatar_key=:avatar_key
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key,
             "enabled": bool(enabled_), "actor": actor[:64]},
        )
        if result.rowcount:
            _audit(conn, org_id, avatar_key, "enabled_set", actor,
                   {"enabled": bool(enabled_)})
    return bool(result.rowcount)


def current_overlay(
    org_id: str, avatar_key: str
) -> Optional[dict[str, Any]]:
    """The currently PUBLISHED overlay for (org, avatar) with provenance, or
    None (no row, disabled, or nothing published). The resolver's one read."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT v.version, v.overlay_json
                FROM org_avatars a
                JOIN org_avatar_versions v
                  ON v.org_id=a.org_id AND v.avatar_key=a.avatar_key
                  AND v.version=a.current_version
                WHERE a.org_id=:org_id AND a.avatar_key=:avatar_key
                  AND a.enabled AND a.current_version > 0
                  AND v.status='published'
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key},
        ).mappings().first()
    if row is None:
        return None
    return {"version": int(row["version"]),
            "overlay": _overlay_json(row["overlay_json"])}


# ── assignments ─────────────────────────────────────────────────────────────

def upsert_assignment(
    org_id: str, avatar_key: str, scope_kind: str, scope_value: str,
    actor: str,
) -> tuple[Optional[dict[str, Any]], str]:
    if scope_kind not in ("org_default", "user"):
        return None, "unknown_scope_kind"
    if scope_kind == "org_default":
        scope_value = ""
    elif not str(scope_value or "").strip():
        return None, "scope_value_required"
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        target = conn.execute(
            text(
                "SELECT 1 FROM org_avatars "
                "WHERE org_id=:org_id AND avatar_key=:avatar_key"
            ),
            {"org_id": org_id, "avatar_key": avatar_key},
        ).first()
        if target is None:
            return None, "unknown_avatar"
        # One active assignment per scope: retire any existing one for the
        # same scope (possibly pointing at another avatar), then insert.
        conn.execute(
            text(
                """
                UPDATE org_avatar_assignments
                SET active=false, updated_at=clock_timestamp()
                WHERE org_id=:org_id AND scope_kind=:scope_kind
                  AND scope_value=:scope_value AND active
                """
            ),
            {"org_id": org_id, "scope_kind": scope_kind,
             "scope_value": scope_value[:120]},
        )
        row = conn.execute(
            text(
                """
                INSERT INTO org_avatar_assignments (
                  org_id, avatar_key, scope_kind, scope_value, created_by
                ) VALUES (
                  :org_id, :avatar_key, :scope_kind, :scope_value, :actor
                )
                RETURNING id::text, avatar_key, scope_kind, scope_value
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key,
             "scope_kind": scope_kind, "scope_value": scope_value[:120],
             "actor": actor[:64]},
        ).mappings().one()
        _audit(conn, org_id, avatar_key, "assignment_set", actor,
               {"scope_kind": scope_kind, "scope_value": scope_value[:120]})
    return dict(row), ""


def delete_assignment(org_id: str, assignment_id: str, actor: str) -> bool:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                UPDATE org_avatar_assignments
                SET active=false, updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:id AS uuid) AND active
                RETURNING avatar_key, scope_kind, scope_value
                """
            ),
            {"org_id": org_id, "id": assignment_id},
        ).mappings().first()
        if row is not None:
            _audit(conn, org_id, str(row["avatar_key"]),
                   "assignment_removed", actor,
                   {"scope_kind": str(row["scope_kind"]),
                    "scope_value": str(row["scope_value"])})
    return row is not None


def list_assignments(org_id: str) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT id::text, avatar_key, scope_kind, scope_value,
                       priority, extract(epoch from created_at) AS created_at
                FROM org_avatar_assignments
                WHERE org_id=:org_id AND active
                ORDER BY scope_kind, scope_value
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def assignment_for(
    org_id: str, *, principal_id: str = ""
) -> Optional[dict[str, Any]]:
    """The applicable assignment under the deterministic precedence
    user > org_default (an explicit avatar on the session request is handled
    by the caller and always wins before this is consulted)."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        if principal_id:
            row = conn.execute(
                text(
                    """
                    SELECT id::text, avatar_key, scope_kind, scope_value
                    FROM org_avatar_assignments
                    WHERE org_id=:org_id AND active
                      AND scope_kind='user' AND scope_value=:principal
                    """
                ),
                {"org_id": org_id, "principal": principal_id[:120]},
            ).mappings().first()
            if row is not None:
                return dict(row)
        row = conn.execute(
            text(
                """
                SELECT id::text, avatar_key, scope_kind, scope_value
                FROM org_avatar_assignments
                WHERE org_id=:org_id AND active
                  AND scope_kind='org_default'
                """
            ),
            {"org_id": org_id},
        ).mappings().first()
    return dict(row) if row is not None else None


def list_audit(org_id: str, avatar_key: str = "", limit: int = 50) -> list[dict]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        key_filter = "AND avatar_key=:avatar_key" if avatar_key else ""
        rows = conn.execute(
            text(
                f"""
                SELECT avatar_key, action, actor, detail_json,
                       extract(epoch from at) AS at
                FROM org_avatar_audit
                WHERE org_id=:org_id {key_filter}
                ORDER BY at DESC, id DESC
                LIMIT :limit
                """
            ),
            {"org_id": org_id, "avatar_key": avatar_key,
             "limit": max(1, min(int(limit), 200))},
        ).mappings().all()
    return [dict(r) for r in rows]
