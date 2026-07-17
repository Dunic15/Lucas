"""DF sync worker + retention completion (accepted contract v5).

One tick: claim due runs per due org (SKIP LOCKED + lease), run the
connector, apply the batch + cursor ATOMICALLY, then reconcile retrieval
(chunk removal for tombstoned/ACL-revoked body docs + index rebuild). Failure
paths per contract: ScopeLost -> connector needs_reconnect + run failed and
NOT authoritative; Backpressure -> run parks (quarantine cap, nothing
deleted); other errors retry on the ladder then park, dead-letter after 3
parks. The two-phase purge audits complete here too (storage deletion first,
then the audit row is marked done — retry-safe).

Runs in the main.py lifespan loop, flag-gated, never on the live path.
"""
from __future__ import annotations

from . import connectors as connectors_mod
from . import dal


def _reconcile_retrieval(org_id: str, affected_docs: list[str]) -> None:
    """Post-commit retrieval invalidation: remove chunks for documents whose
    DF head is no longer live-index-eligible, then converge the org index."""
    if not affected_docs:
        return
    try:
        from .. import knowledge
        from ..knowledge import dal as kdal
        from ..knowledge import ingest as kingest

        if not knowledge.enabled():
            return
        restricted = dal.restricted_document_ids(org_id)
        from sqlalchemy import text

        from .. import control_plane

        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            for doc_id in affected_docs:
                if doc_id in restricted:
                    conn.execute(
                        text(
                            "DELETE FROM knowledge_chunks "
                            "WHERE org_id=:org_id "
                            "AND document_id=CAST(:doc AS uuid)"
                        ),
                        {"org_id": org_id, "doc": doc_id},
                    )
        sources = kdal.list_sources(org_id)
        if sources:
            kdal.enqueue_job(org_id, sources[0]["id"], "rebuild_index")
        else:
            kingest.rebuild_indexes(org_id)
    except Exception as exc:  # noqa: BLE001 — the epoch sweep is the backstop
        print(f"[df] retrieval reconcile failed: {type(exc).__name__}",
              flush=True)


def materialize_bodies(org_id: str, connector: dict,
                       envelopes: list[dict]) -> list[dict]:
    """Contract: body-bearing envelopes reuse the knowledge pipeline AS A
    LIBRARY. Each body_text lands as a document in a per-connector knowledge
    source (named ``df:<kind>:<name>``, NOT avatar-assigned — live-index
    exposure stays governed by explicit assignment + the org_default rule),
    and the envelope gains its ``body_ref``. With the Brain flag off,
    body-bearing envelopes are rewritten into guaranteed-invalid form with
    reason ``body_pipeline_disabled`` so they QUARANTINE instead of failing
    the run. Structured/no-body envelopes pass through untouched."""
    from .. import knowledge

    out: list[dict] = []
    brain_on = knowledge.enabled()
    source_id = ""
    for env in envelopes:
        body = env.get("body_text") if isinstance(env, dict) else None
        if not body:
            out.append(env)
            continue
        if not brain_on:
            out.append({**env, "kind": "__body_pipeline_disabled__"})
            continue
        try:
            from ..knowledge import dal as kdal
            from ..knowledge import ingest as kingest
            from ..knowledge import storage as kstorage

            if not source_id:
                source = _ensure_df_source(org_id, connector)
                source_id = source["id"]
            doc = kdal.upsert_document(
                org_id, source_id,
                f"{env.get('external_id')}.md",
                mime=str(env.get("mime") or "text/plain"),
                size_bytes=len(body.encode()),
            )
            if doc is None:
                out.append({**env, "kind": "__body_source_missing__"})
                continue
            ref = kstorage.put_bytes(org_id, doc["id"],
                                     f"{env.get('external_id')}.md",
                                     body.encode())
            kdal.set_document_storage(org_id, doc["id"], ref)
            chunks = kingest.chunk_text(f"{env.get('external_id')}.md", body)
            from ..knowledge.ingest import _checksum as k_checksum

            result = kdal.publish_version(
                org_id, doc["id"], checksum=k_checksum(body),
                text_content=body[:400000], chunks=chunks,
            )
            version_id = _latest_version_id(org_id, doc["id"])
            out.append({**env,
                        "body_ref": f"kdv:{version_id}:{doc['id']}"
                        if version_id else "",
                        "body_text": None})
            _ = result
        except Exception:  # noqa: BLE001 — this envelope quarantines
            out.append({**env, "kind": "__body_ingest_failed__"})
    return out


def _ensure_df_source(org_id: str, connector: dict) -> dict:
    from ..knowledge import dal as kdal

    name = f"df:{connector.get('kind')}:{connector.get('name')}"[:120]
    for source in kdal.list_sources(org_id):
        if source["name"] == name:
            return source
    created = kdal.create_source(org_id, name, "upload")
    assert created is not None
    return created


def _latest_version_id(org_id: str, document_id: str) -> str:
    from sqlalchemy import text

    from .. import control_plane

    engine = control_plane._get_engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org_id)
        row = conn.execute(
            text(
                "SELECT id::text FROM knowledge_document_versions "
                "WHERE org_id=:org_id AND document_id=CAST(:doc AS uuid) "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"org_id": org_id, "doc": document_id},
        ).first()
    return str(row[0]) if row else ""


def process_due(max_orgs: int = 5, runs_per_org: int = 2) -> int:
    handled = 0
    for org_id in dal.due_orgs(max_orgs):
        for run in dal.claim_due_runs(org_id, runs_per_org):
            handled += 1
            connector = dal.get_connector(org_id, run["connector_id"])
            if connector is None:
                dal.finish_run(org_id, run["id"], run["lease_token"],
                               outcome="dead_letter",
                               error="connector missing")
                continue
            if connector["status"] != "active":
                dal.finish_run(org_id, run["id"], run["lease_token"],
                               outcome="park",
                               error=f"connector {connector['status']}",
                               parks=int(run["parks"]))
                continue
            try:
                impl = connectors_mod.get(connector["kind"])
                cursor = dal.get_cursor(org_id, run["connector_id"])
                batch = impl.sync(org_id, connector, cursor,
                                  full=(run["kind"] == "full"))
                envelopes = materialize_bodies(org_id, connector,
                                               batch.envelopes)
                stats = dal.commit_batch(
                    org_id, run["connector_id"], envelopes,
                    new_cursor=batch.new_cursor, sync_run_id=int(run["id"]),
                    identities=batch.identities,
                    trusted_email_issuer=bool(
                        connector.get("trusted_email_issuer")
                    ),
                    quarantine_payload_ref=_payload_ref_writer(org_id),
                )
                affected = stats.pop("affected_docs", [])
                if connector["kind"] == "gdrive":
                    # An authoritative mirrored sync marks ACL as mirrored.
                    dal.set_connector_acl_mirrored(
                        org_id, run["connector_id"], True
                    )
                dal.finish_run(org_id, run["id"], run["lease_token"],
                               outcome="done", stats=stats)
                _reconcile_retrieval(org_id, affected)
            except dal.ScopeLostError as exc:
                # Contract: NOT reported successful-authoritative; connector
                # leaves eligibility immediately (visibility rule).
                dal.set_connector_status(org_id, run["connector_id"],
                                         "needs_reconnect", actor="sync")
                dal.finish_run(org_id, run["id"], run["lease_token"],
                               outcome="retry",
                               error=f"scope_lost: {exc}",
                               attempts=int(run["attempts"]))
            except dal.BackpressureError as exc:
                dal.finish_run(org_id, run["id"], run["lease_token"],
                               outcome="park",
                               error=f"quarantine_backpressure: {exc}",
                               parks=int(run["parks"]))
            except connectors_mod.ConnectorNotImplemented as exc:
                dal.finish_run(org_id, run["id"], run["lease_token"],
                               outcome="park",
                               error=f"not_implemented: {exc}",
                               parks=int(run["parks"]))
            except Exception as exc:  # noqa: BLE001 — CLASS NAME ONLY
                # A raw driver exception can carry failing-row column values
                # (incl. record titles/PII). Persist the class name only —
                # last_error is surfaced on GET /org/data/runs.
                dal.finish_run(org_id, run["id"], run["lease_token"],
                               outcome="retry",
                               error=type(exc).__name__,
                               attempts=int(run["attempts"]),
                               parks=int(run["parks"]))
    return handled


def _payload_ref_writer(org_id: str):
    """Quarantined envelopes persist through the storage adapter as opaque
    refs (never inline PII in quarantine rows)."""
    def write(raw) -> str:
        import json
        import uuid as _uuid

        from ..knowledge import storage

        data = json.dumps(raw, separators=(",", ":"),
                          default=str).encode()[:200000]
        return storage.put_bytes(org_id, f"quarantine-{_uuid.uuid4().hex}",
                                 "envelope.json", data)
    return write


def complete_purge_audits(org_id: str) -> int:
    """Two-phase step 2: delete the storage objects a purge left pending,
    then mark the audit row completed. Retry-safe: a failed storage delete
    leaves the row pending for the next pass."""
    from ..knowledge import storage

    completed = 0
    for audit in dal.pending_purge_audits(org_id):
        ok = True
        for ref in audit["payload_refs_pending"]:
            try:
                storage.delete_ref(str(ref))
            except Exception:  # noqa: BLE001
                ok = False
                break
        if ok and dal.complete_purge_audit(org_id, int(audit["id"])):
            completed += 1
    return completed
