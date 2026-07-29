"""M2 datastore: connector sync state, normalized ACLs, identity, audit,
and the ACL-filtered retrieval candidate queries (tables from migration 0011).

Same discipline as dal.py: transaction-local ``app.current_org`` before the
first statement, ``org_id`` repeated in every predicate even though FORCE RLS
would catch it, no content/token logging anywhere. Principal expansion and
document visibility are SQL — the ACL filter runs INSIDE the candidate query,
before any text leaves the store (THREAT-MODEL.md TB3).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy import bindparam, text

from .. import control_plane


def _engine():
    return control_plane._get_engine()


def _set_org(conn, org_id: str) -> None:
    control_plane._set_org(conn, org_id)


# ── source connection state ─────────────────────────────────────────────────

def set_source_config(org_id: str, source_id: str, config: dict) -> bool:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                """
                UPDATE knowledge_sources
                SET config_json=:config, updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:source_id AS uuid)
                """
            ),
            {"org_id": org_id, "source_id": source_id,
             "config": json.dumps(config or {})[:8000]},
        )
    return bool(result.rowcount)


def get_source_ext(org_id: str, source_id: str) -> Optional[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT id::text, name, kind, status, config_json,
                       connection_status, last_sync_error,
                       embedding_provider,
                       extract(epoch from last_sync_at)::float8 AS last_sync_at
                FROM knowledge_sources
                WHERE org_id=:org_id AND id=CAST(:source_id AS uuid)
                """
            ),
            {"org_id": org_id, "source_id": source_id},
        ).mappings().first()
    if row is None:
        return None
    out = dict(row)
    try:
        out["config"] = json.loads(out.pop("config_json") or "{}")
    except ValueError:
        out["config"] = {}
    return out


def set_connection_status(
    org_id: str, source_id: str, status: str, error: str = ""
) -> bool:
    if status not in ("pending_scope", "active", "revoked", "error"):
        return False
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                """
                UPDATE knowledge_sources
                SET connection_status=:status, last_sync_error=:error,
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:source_id AS uuid)
                """
            ),
            {"org_id": org_id, "source_id": source_id, "status": status,
             "error": str(error or "")[:300]},
        )
    return bool(result.rowcount)


def mark_sync_success(org_id: str, source_id: str) -> None:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                """
                UPDATE knowledge_sources
                SET last_sync_at=clock_timestamp(), last_sync_error='',
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:source_id AS uuid)
                """
            ),
            {"org_id": org_id, "source_id": source_id},
        )


# ── durable opaque checkpoints ──────────────────────────────────────────────

def get_checkpoint(org_id: str, source_id: str, resource_key: str) -> str:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT checkpoint FROM knowledge_sync_state
                WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)
                  AND resource_key=:rk
                """
            ),
            {"org_id": org_id, "source_id": source_id, "rk": resource_key},
        ).first()
    return str(row[0]) if row else ""


def save_checkpoint(
    org_id: str, source_id: str, resource_key: str, checkpoint: str
) -> None:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                """
                INSERT INTO knowledge_sync_state (
                  org_id, source_id, resource_key, checkpoint, updated_at
                ) VALUES (
                  :org_id, CAST(:source_id AS uuid), :rk, :cp,
                  clock_timestamp()
                )
                ON CONFLICT (org_id, source_id, resource_key)
                DO UPDATE SET checkpoint=excluded.checkpoint,
                              updated_at=clock_timestamp()
                """
            ),
            {"org_id": org_id, "source_id": source_id, "rk": resource_key,
             "cp": str(checkpoint or "")},
        )


# ── principals, membership, identity ────────────────────────────────────────

def upsert_principals(
    org_id: str, source_id: str, principals: list[dict[str, Any]]
) -> dict[tuple[str, str], str]:
    """Upsert (kind, external_id) principals; returns the id map used to
    resolve permissions and edges."""
    out: dict[tuple[str, str], str] = {}
    if not principals:
        return out
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        for p in principals:
            kind = str(p.get("kind") or "")
            ext = str(p.get("external_id") or "")
            if not kind or not ext:
                continue
            row = conn.execute(
                text(
                    """
                    INSERT INTO knowledge_principals (
                      org_id, source_id, external_id, kind, display, email
                    ) VALUES (
                      :org_id, CAST(:source_id AS uuid), :ext, :kind,
                      :display, :email
                    )
                    ON CONFLICT (org_id, source_id, kind, external_id)
                    DO UPDATE SET display=excluded.display,
                                  email=excluded.email,
                                  updated_at=clock_timestamp()
                    RETURNING id::text
                    """
                ),
                {"org_id": org_id, "source_id": source_id, "ext": ext[:200],
                 "kind": kind, "display": str(p.get("display") or "")[:200],
                 "email": str(p.get("email") or "").lower()[:200]},
            ).first()
            out[(kind, ext)] = str(row[0])
    return out


def replace_group_edges(
    org_id: str, source_id: str, edges: list[tuple[str, str]],
    pmap: dict[tuple[str, str], str],
) -> int:
    """Wholesale-replace this connection's membership edges (drift in group
    membership is a permission change — THREAT-MODEL.md T2)."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                "DELETE FROM knowledge_group_edges "
                "WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)"
            ),
            {"org_id": org_id, "source_id": source_id},
        )
        count = 0
        for group_ext, member_ext in edges:
            group_id = pmap.get(("group", group_ext))
            member_id = (
                pmap.get(("user", member_ext)) or pmap.get(("group", member_ext))
            )
            if not group_id or not member_id:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO knowledge_group_edges (
                      org_id, source_id, group_id, member_id
                    ) VALUES (
                      :org_id, CAST(:source_id AS uuid),
                      CAST(:group_id AS uuid), CAST(:member_id AS uuid)
                    )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"org_id": org_id, "source_id": source_id,
                 "group_id": group_id, "member_id": member_id},
            )
            count += 1
    return count


def upsert_identity(
    org_id: str, source_id: str, user_key: str, principal_external_id: str,
    *, kind: str = "user",
) -> bool:
    """Map a Laura caller (lowercased email) to a source principal. False
    when the principal doesn't exist — never create one from a guess."""
    key = str(user_key or "").strip().lower()
    if not key:
        return False
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT id::text FROM knowledge_principals
                WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)
                  AND kind=:kind AND external_id=:ext
                """
            ),
            {"org_id": org_id, "source_id": source_id, "kind": kind,
             "ext": principal_external_id},
        ).first()
        if row is None:
            return False
        conn.execute(
            text(
                """
                INSERT INTO knowledge_identity_map (
                  org_id, source_id, user_key, principal_id
                ) VALUES (
                  :org_id, CAST(:source_id AS uuid), :user_key,
                  CAST(:principal_id AS uuid)
                )
                ON CONFLICT (org_id, source_id, user_key)
                DO UPDATE SET principal_id=excluded.principal_id
                """
            ),
            {"org_id": org_id, "source_id": source_id, "user_key": key,
             "principal_id": str(row[0])},
        )
    return True


_MAX_GROUP_DEPTH = 8


def principal_ids_for_user(org_id: str, user_key: str) -> list[str]:
    """The caller's expanded principal set: mapped identities, every group
    reachable through membership edges (cycle-safe, depth-capped), plus the
    tenant/everyone principals of connections the caller is mapped into.
    Empty list (no mapping) = default deny."""
    key = str(user_key or "").strip().lower()
    if not key:
        return []
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                WITH RECURSIVE seed AS (
                  SELECT im.principal_id AS pid
                  FROM knowledge_identity_map im
                  JOIN knowledge_sources s
                    ON s.org_id=im.org_id AND s.id=im.source_id
                  WHERE im.org_id=:org_id AND im.user_key=:user_key
                    AND s.status='active' AND s.connection_status='active'
                ), expand AS (
                  SELECT pid, 0 AS depth FROM seed
                  UNION
                  SELECT e.group_id, x.depth + 1
                  FROM knowledge_group_edges e
                  JOIN expand x ON e.member_id = x.pid
                  WHERE e.org_id=:org_id AND x.depth < :max_depth
                )
                SELECT DISTINCT pid::text FROM expand
                UNION
                SELECT p.id::text
                FROM knowledge_principals p
                JOIN knowledge_sources s
                  ON s.org_id=p.org_id AND s.id=p.source_id
                WHERE p.org_id=:org_id
                  AND p.kind IN ('tenant', 'everyone')
                  AND s.status='active' AND s.connection_status='active'
                  AND EXISTS (
                    SELECT 1 FROM knowledge_identity_map im2
                    WHERE im2.org_id=p.org_id AND im2.source_id=p.source_id
                      AND im2.user_key=:user_key
                  )
                """
            ),
            {"org_id": org_id, "user_key": key,
             "max_depth": _MAX_GROUP_DEPTH},
        ).fetchall()
    return [str(r[0]) for r in rows]


def org_public_principal_ids(org_id: str) -> list[str]:
    """Tenant-wide/everyone principals of active connections — the widest
    audience an unbound (no human user) caller can ever get, gated by the
    deployment-wide settings.knowledge_meeting_audience switch at the call
    sites (retrieval.meeting_search / router._brain_query)."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT p.id::text
                FROM knowledge_principals p
                JOIN knowledge_sources s
                  ON s.org_id=p.org_id AND s.id=p.source_id
                WHERE p.org_id=:org_id AND p.kind IN ('tenant', 'everyone')
                  AND s.status='active' AND s.connection_status='active'
                """
            ),
            {"org_id": org_id},
        ).fetchall()
    return [str(r[0]) for r in rows]


# ── canonical documents (connector identity = external_id) ──────────────────

def upsert_document_ext(
    org_id: str, source_id: str, item: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Upsert a connector document by stable external id. Returns
    {"id", "content_changed"}; revives tombstones when the source restores an
    item. Metadata always refreshes; content re-ingest only when the source
    version moved (re-chunk/re-embed without re-download stays possible via
    the stored bytes)."""
    ext = str(item.get("external_id") or "")
    if not ext:
        return None
    params = {
        "org_id": org_id, "source_id": source_id, "ext": ext[:300],
        "filename": str(item.get("title") or ext)[:200],
        "title": str(item.get("title") or "")[:300],
        "web_url": str(item.get("web_url") or "")[:600],
        "mime": str(item.get("mime") or "")[:100],
        "version": str(item.get("source_version") or "")[:120],
        "modified": float(item.get("modified_at") or 0.0) or None,
        "author": str(item.get("author") or "")[:200],
        "parent_ref": str(item.get("parent_ref") or "")[:600],
        "size": int(item.get("size") or 0),
    }
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        existing = conn.execute(
            text(
                """
                SELECT id::text, source_version, status
                FROM knowledge_documents
                WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)
                  AND external_id=:ext
                FOR UPDATE
                """
            ),
            {"org_id": org_id, "source_id": source_id, "ext": params["ext"]},
        ).mappings().first()
        if existing is None:
            row = conn.execute(
                text(
                    """
                    INSERT INTO knowledge_documents (
                      org_id, source_id, filename, mime, size_bytes,
                      external_id, title, web_url, source_version,
                      source_modified_at, author, parent_ref, status
                    ) VALUES (
                      :org_id, CAST(:source_id AS uuid), :filename, :mime,
                      :size, :ext, :title, :web_url, :version,
                      to_timestamp(:modified), :author, :parent_ref, 'pending'
                    )
                    RETURNING id::text
                    """
                ),
                params,
            ).mappings().one()
            return {"id": row["id"], "content_changed": True}
        content_changed = (
            not params["version"]
            or existing["source_version"] != params["version"]
            or existing["status"] != "published"
        )
        conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET filename=:filename, title=:title, web_url=:web_url,
                    mime=:mime, size_bytes=:size, source_version=:version,
                    source_modified_at=to_timestamp(:modified),
                    author=:author, parent_ref=:parent_ref,
                    status=CASE WHEN :changed THEN 'pending' ELSE status END,
                    error=CASE WHEN :changed THEN '' ELSE error END,
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:doc_id AS uuid)
                """
            ),
            {**params, "doc_id": existing["id"], "changed": content_changed},
        )
        return {"id": existing["id"], "content_changed": content_changed}


def tombstone_document(org_id: str, source_id: str, external_id: str) -> bool:
    """Remote deletion: tombstone the document and remove retrievability in
    the same transaction (chunks + ACL rows are derived data)."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET status='deleted', updated_at=clock_timestamp()
                WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)
                  AND external_id=:ext AND status <> 'deleted'
                RETURNING id::text
                """
            ),
            {"org_id": org_id, "source_id": source_id,
             "ext": str(external_id or "")[:300]},
        ).first()
        if row is None:
            return False
        doc_id = str(row[0])
        conn.execute(
            text(
                "DELETE FROM knowledge_chunks "
                "WHERE org_id=:org_id AND document_id=CAST(:doc_id AS uuid)"
            ),
            {"org_id": org_id, "doc_id": doc_id},
        )
        conn.execute(
            text(
                "DELETE FROM knowledge_document_acl "
                "WHERE org_id=:org_id AND document_id=CAST(:doc_id AS uuid)"
            ),
            {"org_id": org_id, "doc_id": doc_id},
        )
    return True


def replace_document_acl(
    org_id: str, document_id: str, grants: list[dict[str, Any]]
) -> int:
    """Atomically replace one document's ACL and stamp its freshness. The
    stamp is what retrieval's staleness predicate checks — a document whose
    permissions were never (or too long ago) synced is not retrievable."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                "DELETE FROM knowledge_document_acl "
                "WHERE org_id=:org_id AND document_id=CAST(:doc_id AS uuid)"
            ),
            {"org_id": org_id, "doc_id": document_id},
        )
        count = 0
        seen: set[str] = set()
        for g in grants:
            pid = str(g.get("principal_id") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            conn.execute(
                text(
                    """
                    INSERT INTO knowledge_document_acl (
                      org_id, document_id, principal_id, role, inherited_from
                    ) VALUES (
                      :org_id, CAST(:doc_id AS uuid), CAST(:pid AS uuid),
                      :role, :inherited_from
                    )
                    """
                ),
                {"org_id": org_id, "doc_id": document_id, "pid": pid,
                 "role": str(g.get("role") or "read")[:40],
                 "inherited_from": str(g.get("inherited_from") or "")[:300]},
            )
            count += 1
        conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET acl_synced_at=clock_timestamp(), acl_error=''
                WHERE org_id=:org_id AND id=CAST(:doc_id AS uuid)
                """
            ),
            {"org_id": org_id, "doc_id": document_id},
        )
    return count


def set_acl_error(org_id: str, document_id: str, error: str) -> None:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET acl_error=:error, updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:doc_id AS uuid)
                """
            ),
            {"org_id": org_id, "doc_id": document_id,
             "error": str(error or "")[:300]},
        )


# ── retrieval candidates (tenant + ACL filter INSIDE the query) ─────────────

_CANDIDATE_FILTER = """
    FROM knowledge_chunks c
    JOIN knowledge_documents d
      ON d.org_id=c.org_id AND d.id=c.document_id
    JOIN knowledge_sources s
      ON s.org_id=c.org_id AND s.id=c.source_id
    WHERE c.org_id=:org_id
      AND s.status='active' AND s.connection_status='active'
      AND d.status='published'
      AND d.acl_synced_at IS NOT NULL
      AND d.acl_error = ''
      AND d.acl_synced_at >= clock_timestamp()
            - (:stale_seconds * interval '1 second')
      AND EXISTS (
        SELECT 1 FROM knowledge_document_acl a
        WHERE a.org_id=c.org_id AND a.document_id=c.document_id
          AND a.principal_id::text IN :pids
      )
"""

_CANDIDATE_COLUMNS = """
    SELECT c.id AS chunk_id, c.text, c.section, c.seq, c.embedding_json,
           d.id::text AS document_id, d.title, d.web_url, d.filename,
           s.name AS source_name, s.embedding_provider,
           extract(epoch from d.source_modified_at)::float8 AS modified_at
"""


def retrieval_candidates(
    org_id: str, principal_ids: list[str], q: str, *,
    stale_seconds: int, fts_limit: int = 50, pool_limit: int = 200,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(FTS-ranked candidates, recency pool for semantic scoring) — both
    already tenant- and ACL-filtered. Empty principal set short-circuits to
    nothing without touching the DB (default deny)."""
    if not principal_ids:
        return [], []
    query = " ".join(str(q or "").split())
    fts_sql = text(
        _CANDIDATE_COLUMNS
        + ", ts_rank(c.fts, plainto_tsquery('simple', :q)) AS rank "
        + _CANDIDATE_FILTER
        + " AND c.fts @@ plainto_tsquery('simple', :q)"
        + " ORDER BY rank DESC, c.id LIMIT :fts_limit"
    ).bindparams(bindparam("pids", expanding=True))
    pool_sql = text(
        _CANDIDATE_COLUMNS + ", 0.0 AS rank "
        + _CANDIDATE_FILTER
        + " AND c.embedding_json <> ''"
        + " ORDER BY d.source_modified_at DESC NULLS LAST, c.id"
        + " LIMIT :pool_limit"
    ).bindparams(bindparam("pids", expanding=True))
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        base = {"org_id": org_id, "pids": list(principal_ids),
                "stale_seconds": int(stale_seconds)}
        fts_rows = []
        if query:
            fts_rows = conn.execute(
                fts_sql, {**base, "q": query, "fts_limit": int(fts_limit)},
            ).mappings().all()
        pool_rows = conn.execute(
            pool_sql, {**base, "pool_limit": int(pool_limit)},
        ).mappings().all()
    return [dict(r) for r in fts_rows], [dict(r) for r in pool_rows]


# ── audit (append-only) ─────────────────────────────────────────────────────

def audit(org_id: str, actor: str, event: str, detail: dict | None = None) -> None:
    try:
        engine = _engine()
        with engine.begin() as conn:
            _set_org(conn, org_id)
            conn.execute(
                text(
                    """
                    INSERT INTO knowledge_audit (org_id, actor, event, detail_json)
                    VALUES (:org_id, :actor, :event, :detail)
                    """
                ),
                {"org_id": org_id, "actor": str(actor or "")[:80],
                 "event": str(event or "")[:80],
                 "detail": json.dumps(detail or {}, sort_keys=True)[:2000]},
            )
    except Exception:  # noqa: BLE001 — auditing must never break the data path
        pass


def audit_events(
    org_id: str, *, event: str = "", limit: int = 100
) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT id, extract(epoch from ts)::float8 AS ts, actor, event,
                       detail_json
                FROM knowledge_audit
                WHERE org_id=:org_id
                  AND (:event = '' OR event=:event)
                ORDER BY id DESC LIMIT :limit
                """
            ),
            {"org_id": org_id, "event": str(event or ""),
             "limit": max(1, min(int(limit), 500))},
        ).mappings().all()
    return [dict(r) for r in rows]


def query_hash(q: str) -> str:
    """Audit-safe fingerprint of a query — raw query text is never stored."""
    return hashlib.sha256(str(q or "").encode()).hexdigest()[:12]


# ── connector status page ───────────────────────────────────────────────────

def source_status(org_id: str, source_id: str) -> Optional[dict[str, Any]]:
    src = get_source_ext(org_id, source_id)
    if src is None:
        return None
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        docs = conn.execute(
            text(
                """
                SELECT
                  COUNT(*) FILTER (WHERE status='published') AS published,
                  COUNT(*) FILTER (WHERE status='pending') AS pending,
                  COUNT(*) FILTER (WHERE status='failed') AS failed,
                  COUNT(*) FILTER (WHERE status='deleted') AS tombstoned,
                  COUNT(*) FILTER (
                    WHERE status='published' AND acl_synced_at IS NULL
                  ) AS published_without_acl,
                  extract(epoch from MIN(acl_synced_at) FILTER (
                    WHERE status='published'
                  ))::float8 AS oldest_acl_synced_at
                FROM knowledge_documents
                WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)
                """
            ),
            {"org_id": org_id, "source_id": source_id},
        ).mappings().one()
        jobs = conn.execute(
            text(
                """
                SELECT COUNT(*) FILTER (
                         WHERE status IN ('pending', 'running')
                       ) AS in_flight,
                       COUNT(*) FILTER (
                         WHERE status='failed' AND next_attempt_at IS NULL
                       ) AS parked
                FROM knowledge_sync_jobs
                WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)
                  AND kind='connector_sync'
                """
            ),
            {"org_id": org_id, "source_id": source_id},
        ).mappings().one()
        checkpoints = conn.execute(
            text(
                """
                SELECT resource_key,
                       (checkpoint <> '') AS has_checkpoint,
                       extract(epoch from updated_at)::float8 AS updated_at
                FROM knowledge_sync_state
                WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)
                ORDER BY resource_key
                """
            ),
            {"org_id": org_id, "source_id": source_id},
        ).mappings().all()
    return {
        "source_id": src["id"],
        "name": src["name"],
        "kind": src["kind"],
        "connected": src["connection_status"] == "active",
        "connection_status": src["connection_status"],
        "last_sync_at": src["last_sync_at"],
        "last_sync_error": src["last_sync_error"],
        "documents": {k: int(docs[k] or 0) for k in
                      ("published", "pending", "failed", "tombstoned")},
        "permission_health": {
            "published_without_acl": int(docs["published_without_acl"] or 0),
            "oldest_acl_synced_at": docs["oldest_acl_synced_at"],
        },
        "jobs": {"in_flight": int(jobs["in_flight"] or 0),
                 "parked": int(jobs["parked"] or 0)},
        "resources": [dict(c) for c in checkpoints],
    }


# ── job rescheduling (throttle ≠ failure) ───────────────────────────────────

def reschedule_job(
    org_id: str, job_id: int, lease_token: str, delay_seconds: float,
    note: str = "",
) -> bool:
    """Release a claimed job back to pending after `delay_seconds` without
    burning a retry attempt — the sync engine uses this to honor Retry-After."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                """
                UPDATE knowledge_sync_jobs
                SET status='pending',
                    next_attempt_at=clock_timestamp()
                      + (:delay * interval '1 second'),
                    lease_token=NULL, lease_until=NULL,
                    last_error=:note, updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=:job_id AND status='running'
                  AND lease_token=CAST(:lease_token AS uuid)
                """
            ),
            {"org_id": org_id, "job_id": int(job_id),
             "lease_token": lease_token,
             "delay": max(1.0, float(delay_seconds)),
             "note": str(note or "")[:200]},
        )
    return bool(result.rowcount)
