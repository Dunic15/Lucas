"""Postgres DAL for the Company Brain — outbox_pg's discipline throughout.

Every tenant operation sets transaction-local app.current_org before its
first query, so the laura_app runtime role stays FORCE-RLS bound. Cross-org
discovery (due jobs, boot index rebuild) goes through the two SECURITY
DEFINER functions from migration 0010, which return org UUIDs only.

Nothing here logs document content, filenames beyond errors, or tokens.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

from .. import control_plane
from ..config import settings


def _engine():
    return control_plane._get_engine()


def _set_org(conn, org_id: str) -> None:
    control_plane._set_org(conn, org_id)


# ── sources ─────────────────────────────────────────────────────────────────

def create_source(
    org_id: str, name: str, kind: str, drive_folder_id: str = ""
) -> Optional[dict[str, Any]]:
    if kind not in ("upload", "drive"):
        return None
    from .. import embeddings

    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                INSERT INTO knowledge_sources (
                  org_id, name, kind, drive_folder_id,
                  embedding_provider, embedding_model, embedding_dim
                ) VALUES (
                  :org_id, :name, :kind, :drive_folder_id,
                  :provider, :model, 512
                )
                RETURNING id::text, name, kind, status, drive_folder_id,
                          extract(epoch from created_at)::float8 AS created_at
                """
            ),
            {
                "org_id": org_id,
                "name": str(name or "").strip()[:120] or "Untitled source",
                "kind": kind,
                "drive_folder_id": str(drive_folder_id or "").strip()[:128],
                "provider": embeddings.provider_signature(),
                "model": settings.embedding_model,
            },
        ).mappings().one()
    return dict(row)


def list_sources(org_id: str) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT s.id::text, s.name, s.kind, s.status, s.drive_folder_id,
                       s.embedding_provider, s.embedding_model,
                       extract(epoch from s.created_at)::float8 AS created_at,
                       COUNT(d.id) FILTER (
                         WHERE d.status = 'published'
                       ) AS published_documents,
                       COUNT(d.id) FILTER (
                         WHERE d.status = 'pending'
                       ) AS pending_documents,
                       COUNT(d.id) FILTER (
                         WHERE d.status = 'failed'
                       ) AS failed_documents,
                       COALESCE(
                         array_agg(DISTINCT a.avatar_id)
                           FILTER (WHERE a.avatar_id IS NOT NULL), '{}'
                       ) AS avatars
                FROM knowledge_sources s
                LEFT JOIN knowledge_documents d
                  ON d.org_id = s.org_id AND d.source_id = s.id
                  AND d.status <> 'deleted'
                LEFT JOIN knowledge_assignments a
                  ON a.org_id = s.org_id AND a.source_id = s.id
                WHERE s.org_id = :org_id AND s.status = 'active'
                GROUP BY s.org_id, s.id
                ORDER BY s.created_at DESC
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
    return [
        {**dict(r), "avatars": sorted(list(r["avatars"] or []))} for r in rows
    ]


def get_source(org_id: str, source_id: str) -> Optional[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT id::text, name, kind, status, drive_folder_id
                FROM knowledge_sources
                WHERE org_id=:org_id AND id=CAST(:source_id AS uuid)
                """
            ),
            {"org_id": org_id, "source_id": source_id},
        ).mappings().first()
    return dict(row) if row else None


def delete_source(org_id: str, source_id: str) -> bool:
    """Tombstone a source and remove its retrievability NOW: chunks are
    deleted (derived data), documents tombstone, and every assigned avatar's
    index is rebuilt without the source by the enqueued job."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                UPDATE knowledge_sources
                SET status='deleted', deleted_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:source_id AS uuid)
                  AND status='active'
                RETURNING id
                """
            ),
            {"org_id": org_id, "source_id": source_id},
        ).first()
        if row is None:
            return False
        conn.execute(
            text(
                "DELETE FROM knowledge_chunks "
                "WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)"
            ),
            {"org_id": org_id, "source_id": source_id},
        )
        conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET status='deleted', updated_at=clock_timestamp()
                WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)
                """
            ),
            {"org_id": org_id, "source_id": source_id},
        )
        _enqueue_job_conn(conn, org_id, source_id, "rebuild_index")
    return True


# ── documents ───────────────────────────────────────────────────────────────

def upsert_document(
    org_id: str, source_id: str, filename: str, *, mime: str = "",
    size_bytes: int = 0, checksum: str = "",
) -> Optional[dict[str, Any]]:
    """One document per (source, filename); a re-upload becomes a new version
    at ingest time. Returns the document row (id) or None for a bad source."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        src = conn.execute(
            text(
                "SELECT 1 FROM knowledge_sources "
                "WHERE org_id=:org_id AND id=CAST(:source_id AS uuid) "
                "AND status='active'"
            ),
            {"org_id": org_id, "source_id": source_id},
        ).first()
        if src is None:
            return None
        existing = conn.execute(
            text(
                """
                SELECT id::text FROM knowledge_documents
                WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)
                  AND filename=:filename AND status <> 'deleted'
                ORDER BY created_at LIMIT 1
                """
            ),
            {"org_id": org_id, "source_id": source_id,
             "filename": filename},
        ).mappings().first()
        if existing is not None:
            conn.execute(
                text(
                    """
                    UPDATE knowledge_documents
                    SET mime=:mime, size_bytes=:size_bytes,
                        checksum=:checksum, status='pending', error='',
                        updated_at=clock_timestamp()
                    WHERE org_id=:org_id AND id=CAST(:doc_id AS uuid)
                    """
                ),
                {"org_id": org_id, "doc_id": existing["id"], "mime": mime,
                 "size_bytes": int(size_bytes), "checksum": checksum},
            )
            return {"id": existing["id"], "existing": True}
        row = conn.execute(
            text(
                """
                INSERT INTO knowledge_documents (
                  org_id, source_id, filename, mime, size_bytes, checksum
                ) VALUES (
                  :org_id, CAST(:source_id AS uuid), :filename, :mime,
                  :size_bytes, :checksum
                )
                RETURNING id::text
                """
            ),
            {"org_id": org_id, "source_id": source_id,
             "filename": str(filename or "document")[:200], "mime": mime[:100],
             "size_bytes": int(size_bytes), "checksum": checksum[:80]},
        ).mappings().one()
    return {"id": row["id"], "existing": False}


def set_document_storage(org_id: str, document_id: str, storage_ref: str) -> None:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET storage_ref=:storage_ref, updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:doc_id AS uuid)
                """
            ),
            {"org_id": org_id, "doc_id": document_id,
             "storage_ref": storage_ref[:500]},
        )


def get_document(org_id: str, document_id: str) -> Optional[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT id::text, source_id::text, filename, mime, size_bytes,
                       checksum, storage_ref, status, error
                FROM knowledge_documents
                WHERE org_id=:org_id AND id=CAST(:doc_id AS uuid)
                """
            ),
            {"org_id": org_id, "doc_id": document_id},
        ).mappings().first()
    return dict(row) if row else None


def list_documents(org_id: str, source_id: str) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT d.id::text, d.filename, d.mime, d.size_bytes, d.status,
                       d.error, extract(epoch from d.updated_at)::float8 AS updated_at,
                       COALESCE(MAX(v.version), 0) AS latest_version
                FROM knowledge_documents d
                LEFT JOIN knowledge_document_versions v
                  ON v.org_id = d.org_id AND v.document_id = d.id
                WHERE d.org_id=:org_id
                  AND d.source_id=CAST(:source_id AS uuid)
                  AND d.status <> 'deleted'
                GROUP BY d.org_id, d.id
                ORDER BY d.created_at
                """
            ),
            {"org_id": org_id, "source_id": source_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def mark_document_failed(org_id: str, document_id: str, error: str) -> None:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET status='failed', error=:error,
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:doc_id AS uuid)
                """
            ),
            {"org_id": org_id, "doc_id": document_id,
             "error": str(error or "")[:300]},
        )


def publish_version(
    org_id: str, document_id: str, *, checksum: str, text_content: str,
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Transactionally publish one extracted document version + its chunks.

    Immutability + dedupe: identical checksum to the latest version is a
    no-op ({"deduped": True}); new content appends version N+1, replaces the
    document's chunks, and marks the document published — one transaction, so
    retrieval never sees half a version.
    """
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        doc = conn.execute(
            text(
                """
                SELECT source_id::text, filename FROM knowledge_documents
                WHERE org_id=:org_id AND id=CAST(:doc_id AS uuid)
                FOR UPDATE
                """
            ),
            {"org_id": org_id, "doc_id": document_id},
        ).mappings().first()
        if doc is None:
            return {"error": "document missing"}
        latest = conn.execute(
            text(
                """
                SELECT id::text, version, checksum
                FROM knowledge_document_versions
                WHERE org_id=:org_id AND document_id=CAST(:doc_id AS uuid)
                ORDER BY version DESC LIMIT 1
                """
            ),
            {"org_id": org_id, "doc_id": document_id},
        ).mappings().first()
        if latest is not None and latest["checksum"] == checksum:
            conn.execute(
                text(
                    """
                    UPDATE knowledge_documents
                    SET status='published', error='',
                        updated_at=clock_timestamp()
                    WHERE org_id=:org_id AND id=CAST(:doc_id AS uuid)
                    """
                ),
                {"org_id": org_id, "doc_id": document_id},
            )
            return {"deduped": True, "version": int(latest["version"])}
        version = 1 + int(latest["version"] if latest else 0)
        vrow = conn.execute(
            text(
                """
                INSERT INTO knowledge_document_versions (
                  org_id, document_id, version, checksum,
                  extracted_chars, text_content
                ) VALUES (
                  :org_id, CAST(:doc_id AS uuid), :version, :checksum,
                  :chars, :text_content
                )
                RETURNING id::text
                """
            ),
            {"org_id": org_id, "doc_id": document_id, "version": version,
             "checksum": checksum[:80], "chars": len(text_content),
             "text_content": text_content},
        ).mappings().one()
        conn.execute(
            text(
                "DELETE FROM knowledge_chunks "
                "WHERE org_id=:org_id AND document_id=CAST(:doc_id AS uuid)"
            ),
            {"org_id": org_id, "doc_id": document_id},
        )
        for i, chunk in enumerate(chunks):
            conn.execute(
                text(
                    """
                    INSERT INTO knowledge_chunks (
                      org_id, source_id, document_id, version_id, seq,
                      source_name, section, text
                    ) VALUES (
                      :org_id, CAST(:source_id AS uuid),
                      CAST(:doc_id AS uuid), CAST(:version_id AS uuid),
                      :seq, :source_name, :section, :text
                    )
                    """
                ),
                {
                    "org_id": org_id,
                    "source_id": doc["source_id"],
                    "doc_id": document_id,
                    "version_id": vrow["id"],
                    "seq": i,
                    "source_name": str(chunk.get("source") or doc["filename"])[:200],
                    "section": str(chunk.get("section") or "")[:200],
                    "text": str(chunk.get("text") or ""),
                },
            )
        conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET status='published', error='',
                    updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:doc_id AS uuid)
                """
            ),
            {"org_id": org_id, "doc_id": document_id},
        )
    return {"version": version, "chunks": len(chunks)}


# ── assignments ─────────────────────────────────────────────────────────────

def assign(org_id: str, source_id: str, avatar_id: str) -> bool:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        src = conn.execute(
            text(
                "SELECT 1 FROM knowledge_sources "
                "WHERE org_id=:org_id AND id=CAST(:source_id AS uuid) "
                "AND status='active'"
            ),
            {"org_id": org_id, "source_id": source_id},
        ).first()
        if src is None:
            return False
        conn.execute(
            text(
                """
                INSERT INTO knowledge_assignments (org_id, source_id, avatar_id)
                VALUES (:org_id, CAST(:source_id AS uuid), :avatar_id)
                ON CONFLICT DO NOTHING
                """
            ),
            {"org_id": org_id, "source_id": source_id,
             "avatar_id": str(avatar_id or "")[:64]},
        )
        _enqueue_job_conn(conn, org_id, source_id, "rebuild_index")
    return True


def unassign(org_id: str, source_id: str, avatar_id: str) -> bool:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                """
                DELETE FROM knowledge_assignments
                WHERE org_id=:org_id AND source_id=CAST(:source_id AS uuid)
                  AND avatar_id=:avatar_id
                """
            ),
            {"org_id": org_id, "source_id": source_id, "avatar_id": avatar_id},
        )
        _enqueue_job_conn(conn, org_id, source_id, "rebuild_index")
    return bool(result.rowcount)


def assigned_avatars(org_id: str) -> list[str]:
    """Every avatar with at least one active-source assignment in this org."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT a.avatar_id
                FROM knowledge_assignments a
                JOIN knowledge_sources s
                  ON s.org_id=a.org_id AND s.id=a.source_id
                WHERE a.org_id=:org_id AND s.status='active'
                ORDER BY a.avatar_id
                """
            ),
            {"org_id": org_id},
        ).fetchall()
    return [str(r[0]) for r in rows]


def chunks_for_avatar(org_id: str, avatar_id: str) -> list[dict[str, Any]]:
    """Every published chunk this avatar may retrieve (assignment-gated,
    active sources only) — the input to the index rebuild bridge. Carries the
    chunk's ``sid`` (source id) so a resolved avatar's context scope can
    filter retrieval per source (M2 seam)."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT c.text, c.source_name AS source, c.section,
                       c.source_id::text AS sid,
                       c.document_id::text AS did
                FROM knowledge_chunks c
                JOIN knowledge_sources s
                  ON s.org_id=c.org_id AND s.id=c.source_id
                JOIN knowledge_assignments a
                  ON a.org_id=c.org_id AND a.source_id=c.source_id
                JOIN knowledge_documents d
                  ON d.org_id=c.org_id AND d.id=c.document_id
                WHERE c.org_id=:org_id AND a.avatar_id=:avatar_id
                  AND s.status='active' AND d.status='published'
                ORDER BY c.source_id, c.document_id, c.seq
                """
            ),
            {"org_id": org_id, "avatar_id": avatar_id},
        ).mappings().all()
    out = [dict(r) for r in rows]
    # Data Foundation live-index rule (contract v5, structural): documents
    # whose DF head is tombstoned, non-org_default, or owned by an ineligible
    # connector never enter the per-org index — mirrored/unknown content is
    # absent from anything a meeting can speak, BY CONSTRUCTION.
    from .. import datafoundation

    if datafoundation.enabled():
        from ..datafoundation import dal as df_dal

        try:
            restricted = df_dal.restricted_document_ids(org_id)
        except Exception:  # noqa: BLE001 — fail CLOSED, never widen
            return []
        if restricted:
            out = [r for r in out if r.get("did") not in restricted]
    for r in out:
        r.pop("did", None)
    return out


def keyword_search(
    org_id: str, q: str, *, avatar_id: str = "", limit: int = 8
) -> list[dict[str, Any]]:
    """Keyword half of hybrid search: Postgres full-text over the org's
    published chunks (assignment-filtered when an avatar is named), with
    citations. ACL first: every predicate narrows before ranking."""
    query = " ".join(str(q or "").split())
    if not query:
        return []
    avatar_filter = (
        "AND EXISTS (SELECT 1 FROM knowledge_assignments a "
        "WHERE a.org_id=c.org_id AND a.source_id=c.source_id "
        "AND a.avatar_id=:avatar_id)"
        if avatar_id else ""
    )
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                f"""
                SELECT c.text, c.source_name AS source, c.section,
                       c.document_id::text, c.version_id::text,
                       ts_rank(c.fts, plainto_tsquery('simple', :q)) AS rank
                FROM knowledge_chunks c
                JOIN knowledge_sources s
                  ON s.org_id=c.org_id AND s.id=c.source_id
                JOIN knowledge_documents d
                  ON d.org_id=c.org_id AND d.id=c.document_id
                WHERE c.org_id=:org_id
                  AND s.status='active' AND d.status='published'
                  AND c.fts @@ plainto_tsquery('simple', :q)
                  {avatar_filter}
                ORDER BY rank DESC, c.id
                LIMIT :limit
                """
            ),
            {"org_id": org_id, "q": query, "avatar_id": avatar_id,
             "limit": max(1, min(int(limit), 25))},
        ).mappings().all()
    out = [
        {**dict(r), "rank": float(r["rank"]), "text": str(r["text"])[:800]}
        for r in rows
    ]
    # Same DF live-index restriction the retrieval index applies (adversarial
    # finding): keyword search is a read path too — a mirrored/unknown DF
    # body must never surface here either. Fail closed on any DF error.
    from .. import datafoundation

    if datafoundation.enabled():
        from ..datafoundation import dal as df_dal

        try:
            restricted = df_dal.restricted_document_ids(org_id)
        except Exception:  # noqa: BLE001 — never widen on a DF failure
            return []
        if restricted:
            out = [r for r in out if str(r.get("document_id")) not in restricted]
    return out


# ── ingest jobs (claim/lease — the callback_outbox worker pattern) ─────────

_JOB_LEASE_SECONDS = 300
_JOB_RETRY_SECONDS = (5.0, 30.0, 120.0, 600.0)


def _enqueue_job_conn(
    conn, org_id: str, source_id: str, kind: str, document_id: str | None = None
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO knowledge_sync_jobs (
              org_id, source_id, document_id, kind, next_attempt_at
            ) VALUES (
              :org_id, CAST(:source_id AS uuid),
              CAST(:document_id AS uuid), :kind, clock_timestamp()
            )
            """
        ),
        {"org_id": org_id, "source_id": source_id,
         "document_id": document_id, "kind": kind},
    )


def enqueue_job(
    org_id: str, source_id: str, kind: str, document_id: str | None = None
) -> None:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        _enqueue_job_conn(conn, org_id, source_id, kind, document_id)


def due_orgs(limit: int = 20) -> list[str]:
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM laura_private.due_knowledge_orgs(:limit)"),
            {"limit": max(1, min(int(limit), 100))},
        ).fetchall()
    return [str(r[0]) for r in rows]


def knowledge_epoch(org_id: str) -> int:
    """Monotonic retrievability epoch for one org: the max id of its
    rebuild_index jobs. Every operation that changes what an avatar may
    retrieve (ingest publish, assign/unassign, source delete) enqueues one,
    job rows are never deleted, and bigint identities only grow — so a local
    index file stamped with an older epoch is provably stale, no matter which
    App Runner instance wrote it."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                "SELECT COALESCE(MAX(id), 0) FROM knowledge_sync_jobs "
                "WHERE org_id=:org_id AND kind='rebuild_index'"
            ),
            {"org_id": org_id},
        ).first()
    return int(row[0] if row else 0)


def index_orgs(limit: int = 200) -> list[str]:
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM laura_private.knowledge_index_orgs(:limit)"),
            {"limit": max(1, min(int(limit), 1000))},
        ).fetchall()
    return [str(r[0]) for r in rows]


def claim_due_jobs(org_id: str, limit: int = 4) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                WITH due AS (
                  SELECT id FROM knowledge_sync_jobs
                  WHERE org_id=:org_id
                    AND (
                      (status IN ('pending', 'failed')
                       AND next_attempt_at IS NOT NULL
                       AND next_attempt_at <= clock_timestamp())
                      OR (status='running'
                          AND lease_until IS NOT NULL
                          AND lease_until <= clock_timestamp())
                    )
                  ORDER BY
                    CASE WHEN status='running'
                         THEN lease_until ELSE next_attempt_at END, id
                  FOR UPDATE SKIP LOCKED
                  LIMIT :limit
                )
                UPDATE knowledge_sync_jobs AS j
                SET status='running',
                    lease_token=gen_random_uuid(),
                    lease_until=clock_timestamp()
                      + (:lease * interval '1 second'),
                    updated_at=clock_timestamp()
                FROM due WHERE j.id=due.id
                RETURNING j.id, j.org_id::text, j.source_id::text,
                          j.document_id::text, j.kind, j.attempts,
                          j.lease_token::text
                """
            ),
            {"org_id": org_id, "limit": max(1, min(int(limit), 20)),
             "lease": _JOB_LEASE_SECONDS},
        ).mappings().all()
    return [dict(r) for r in rows]


def finish_job(
    org_id: str, job_id: int, lease_token: str, *, ok: bool, error: str = "",
    attempts: int = 0,
) -> bool:
    if ok:
        assignments = (
            "status='done', attempts=attempts+1, next_attempt_at=NULL, "
            "last_error='', lease_token=NULL, lease_until=NULL, "
            "updated_at=clock_timestamp()"
        )
        params: dict[str, Any] = {}
    else:
        retry_index = min(int(attempts), len(_JOB_RETRY_SECONDS) - 1)
        assignments = (
            "status='failed', attempts=attempts+1, "
            "next_attempt_at=CASE WHEN :park THEN NULL "
            "  ELSE clock_timestamp() + (:retry * interval '1 second') END, "
            "last_error=:error, lease_token=NULL, lease_until=NULL, "
            "updated_at=clock_timestamp()"
        )
        params = {
            "park": int(attempts) >= len(_JOB_RETRY_SECONDS),
            "retry": _JOB_RETRY_SECONDS[retry_index],
            "error": str(error or "")[:200],
        }
    params.update({"org_id": org_id, "job_id": int(job_id),
                   "lease_token": lease_token})
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                f"""
                UPDATE knowledge_sync_jobs
                SET {assignments}
                WHERE org_id=:org_id AND id=:job_id
                  AND status='running'
                  AND lease_token=CAST(:lease_token AS uuid)
                """
            ),
            params,
        )
    return bool(result.rowcount)


def job_rows(org_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT j.id, j.source_id::text, j.document_id::text, j.kind,
                       j.status, j.attempts, j.last_error,
                       extract(epoch from j.updated_at)::float8 AS updated_at
                FROM knowledge_sync_jobs j
                WHERE j.org_id=:org_id
                ORDER BY j.created_at DESC, j.id DESC
                LIMIT :limit
                """
            ),
            {"org_id": org_id, "limit": max(1, min(int(limit), 200))},
        ).mappings().all()
    return [dict(r) for r in rows]


def enqueue_boot_rebuilds() -> int:
    """One rebuild job per org that has published chunks — regenerates the
    in-memory index files after a deploy wiped the disk. Idempotent enough:
    rebuilds are cheap and converge."""
    count = 0
    for org in index_orgs():
        try:
            engine = _engine()
            with engine.begin() as conn:
                _set_org(conn, org)
                src = conn.execute(
                    text(
                        "SELECT id::text FROM knowledge_sources "
                        "WHERE org_id=:org_id AND status='active' "
                        "ORDER BY created_at LIMIT 1"
                    ),
                    {"org_id": org},
                ).first()
                if src is None:
                    continue
                pending = conn.execute(
                    text(
                        "SELECT 1 FROM knowledge_sync_jobs "
                        "WHERE org_id=:org_id AND kind='rebuild_index' "
                        "AND status IN ('pending', 'running') LIMIT 1"
                    ),
                    {"org_id": org},
                ).first()
                if pending is not None:
                    continue
                _enqueue_job_conn(conn, org, str(src[0]), "rebuild_index")
                count += 1
        except Exception:  # noqa: BLE001 — boot rebuild is best-effort per org
            continue
    return count


