"""Connector sync engine — turns SourceConnector deltas into canonical state.

Runs only inside claimed `connector_sync` jobs on the existing claim/lease
queue (never the live path). One run:

  directory sync (principals + membership edges)
  → per resource: delta pages under a page budget
      → per item: tombstone | version-unchanged → ACL-only refresh
                  | changed → fetch bytes → extract → chunk → embed → publish
      → permissions fetch → atomic ACL replace (+ freshness stamp)
      → checkpoint save AFTER the page applied (resume/replay-safe)
  → success stamp.

Control flow the queue understands:
  ConnectorThrottled → ThrottledSync: the job goes back to pending after
    retry_after WITHOUT burning a retry attempt (throttle is not failure).
  ConnectorAuthRevoked → connection_status='revoked', job succeeds (a state
    change was recorded); retrieval stops serving the source immediately via
    its query-time connection_status predicate.
  Page budget exhausted → enqueue a continuation job, finish this one.

Webhooks (router) only ENQUEUE these jobs — correctness never depends on
webhook delivery. Document content is untrusted data end to end: extracted,
chunked, cited — never logged, never treated as instructions.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from ..config import settings
from . import dal, datastore, storage
from .connectors import msgraph
from .connectors.base import (
    ConnectorAuthRevoked,
    ConnectorItemGone,
    ConnectorThrottled,
    DeltaPage,
    RemoteItem,
)


class ThrottledSync(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"throttled ({retry_after}s)")
        self.retry_after = max(1.0, float(retry_after))


# Transport factory seam: tests and the offline demo install a fake here;
# production wires GraphHttpTransport with credentials from the secret store
# (never from source config/indexed metadata).
_TRANSPORT_FACTORY: Callable[[str, dict[str, Any]], Any] | None = None


def set_transport_factory(
    factory: Callable[[str, dict[str, Any]], Any] | None,
) -> None:
    global _TRANSPORT_FACTORY
    _TRANSPORT_FACTORY = factory


def _connector_for(org_id: str, source: dict[str, Any]):
    if source["kind"] != "msgraph":
        raise RuntimeError(f"no connector for kind '{source['kind']}'")
    config = source.get("config") or {}
    if _TRANSPORT_FACTORY is not None:
        transport = _TRANSPORT_FACTORY(org_id, source)
    else:
        raise RuntimeError(
            "msgraph transport not configured (production credentials are a "
            "deploy-time concern; tests install a fake via "
            "set_transport_factory)"
        )
    drives = [str(d) for d in (config.get("drives") or []) if d]
    return msgraph.GraphConnector(transport, drives=drives or None)


def _embed_chunks(chunks: list[dict[str, Any]]) -> None:
    """Stamp each chunk with its embedding vector (key-free by default:
    EMBEDDING_PROVIDER=hash). Provider identity is recorded on the source at
    creation; retrieval skips the semantic half on a provider mismatch rather
    than comparing incompatible vectors."""
    from .. import embeddings

    texts = [str(c.get("text") or "") for c in chunks]
    if not texts:
        return
    vectors = embeddings.embed(texts, input_type="document")
    for chunk, vec in zip(chunks, vectors):
        chunk["embedding_json"] = json.dumps(
            [round(float(x), 6) for x in vec]
        )


def _ingest_item(
    org_id: str, source: dict[str, Any], connector, item: RemoteItem,
    doc_id: str,
) -> None:
    from . import ingest

    data = connector.fetch_content(item)
    data = data[: settings.knowledge_max_file_bytes]
    ref = storage.put_bytes(org_id, doc_id, item.title or item.external_id, data)
    dal.set_document_storage(org_id, doc_id, ref)
    text = ingest.extract_text(item.title or item.external_id, data)
    text = text[: settings.knowledge_max_extracted_chars]
    if not text.strip():
        raise RuntimeError("no extractable text")
    chunks = ingest.chunk_text(item.title or item.external_id, text)
    _embed_chunks(chunks)
    result = dal.publish_version(
        org_id, doc_id,
        checksum=hashlib.sha256(text.encode()).hexdigest(),
        text_content=text, chunks=chunks,
    )
    if result.get("error"):
        raise RuntimeError(str(result["error"]))


def _apply_permissions(
    org_id: str, source_id: str, connector, resource_key: str,
    external_id: str, doc_id: str,
    pmap: dict[tuple[str, str], str],
) -> None:
    perms = connector.fetch_permissions(resource_key, external_id)
    grants: list[dict[str, Any]] = []
    fresh: list[dict[str, Any]] = []
    for perm in perms:
        p = perm.principal
        key = (p.kind, p.external_id)
        if key not in pmap:
            fresh.append({"external_id": p.external_id, "kind": p.kind,
                          "display": p.display, "email": p.email})
    if fresh:
        pmap.update(datastore.upsert_principals(org_id, source_id, fresh))
    for perm in perms:
        p = perm.principal
        pid = pmap.get((p.kind, p.external_id))
        if not pid:
            continue
        if p.kind == "link":
            # Anonymous/company links are recorded as principals but never
            # grant retrieval visibility by themselves (default deny).
            continue
        grants.append({"principal_id": pid, "role": perm.role,
                       "inherited_from": perm.inherited_from})
    datastore.replace_document_acl(org_id, doc_id, grants)
    datastore.audit(org_id, "worker", "acl_updated",
                    {"document_id": doc_id, "grants": len(grants)})


def _apply_page(
    org_id: str, source: dict[str, Any], connector, resource_key: str,
    page: DeltaPage, pmap: dict[tuple[str, str], str],
) -> dict[str, int]:
    stats = {"upserts": 0, "tombstones": 0, "acl_only": 0, "failed": 0}
    source_id = source["id"]
    for item in page.items:
        if item.deleted:
            if datastore.tombstone_document(
                org_id, source_id, item.external_id
            ):
                stats["tombstones"] += 1
                datastore.audit(org_id, "worker", "doc_tombstoned",
                                {"external_id": item.external_id})
            continue
        if item.folder:
            continue  # containers carry inheritance, not content
        doc = datastore.upsert_document_ext(
            org_id, source_id,
            {"external_id": item.external_id, "title": item.title,
             "web_url": item.web_url, "mime": item.mime,
             "source_version": item.source_version,
             "modified_at": item.modified_at, "author": item.author,
             "parent_ref": item.parent_ref, "size": item.size},
        )
        if doc is None:
            continue
        if doc["content_changed"]:
            try:
                _ingest_item(org_id, source, connector, item, doc["id"])
                stats["upserts"] += 1
            except ConnectorItemGone:
                datastore.tombstone_document(
                    org_id, source_id, item.external_id
                )
                stats["tombstones"] += 1
                continue
            except (ConnectorThrottled, ConnectorAuthRevoked):
                raise
            except Exception as e:  # noqa: BLE001 — poison item, not poison sync
                dal.mark_document_failed(
                    org_id, doc["id"], f"{type(e).__name__}: {e}"[:200]
                )
                stats["failed"] += 1
                continue
        else:
            stats["acl_only"] += 1
        try:
            _apply_permissions(
                org_id, source_id, connector, resource_key,
                item.external_id, doc["id"], pmap,
            )
        except ConnectorItemGone:
            datastore.tombstone_document(org_id, source_id, item.external_id)
            stats["tombstones"] += 1
        except (ConnectorThrottled, ConnectorAuthRevoked):
            raise
        except Exception as e:  # noqa: BLE001
            # ACL fetch failed: the freshness stamp does NOT move, so the
            # document ages toward (or stays in) default deny.
            datastore.set_acl_error(
                org_id, doc["id"], f"{type(e).__name__}: {e}"[:200]
            )
    return stats


def run_connector_sync(org_id: str, source_id: str) -> dict[str, Any]:
    """One budgeted sync run for one connection. Raises ThrottledSync for the
    queue's reschedule path; every other outcome is a recorded state."""
    source = datastore.get_source_ext(org_id, source_id)
    if source is None or source["status"] != "active":
        return {"skipped": "source inactive"}
    if source["kind"] not in ("msgraph",):
        return {"skipped": f"kind {source['kind']} has no connector sync"}
    if source["connection_status"] == "revoked":
        return {"skipped": "connection revoked"}
    datastore.audit(org_id, "worker", "sync_started", {"source_id": source_id})
    try:
        connector = _connector_for(org_id, source)
        principals, edges = connector.fetch_principals()
        pmap = datastore.upsert_principals(
            org_id, source_id,
            [{"external_id": p.external_id, "kind": p.kind,
              "display": p.display, "email": p.email} for p in principals],
        )
        datastore.replace_group_edges(org_id, source_id, edges, pmap)
        budget = max(1, int(settings.knowledge_sync_max_pages_per_run))
        pages = 0
        totals = {"upserts": 0, "tombstones": 0, "acl_only": 0, "failed": 0}
        for resource_key in connector.list_resources():
            checkpoint = datastore.get_checkpoint(
                org_id, source_id, resource_key
            )
            done = False
            while not done:
                if pages >= budget:
                    dal.enqueue_job(org_id, source_id, "connector_sync")
                    datastore.audit(
                        org_id, "worker", "sync_page",
                        {"source_id": source_id, "pages": pages,
                         "continued": True},
                    )
                    return {"continued": True, "pages": pages, **totals}
                page = connector.delta(resource_key, checkpoint)
                stats = _apply_page(
                    org_id, source, connector, resource_key, page, pmap
                )
                for k in totals:
                    totals[k] += stats[k]
                checkpoint = page.checkpoint
                datastore.save_checkpoint(
                    org_id, source_id, resource_key, checkpoint
                )
                pages += 1
                done = page.done
        datastore.mark_sync_success(org_id, source_id)
        datastore.audit(org_id, "worker", "sync_done",
                        {"source_id": source_id, "pages": pages, **totals})
        return {"pages": pages, **totals}
    except ConnectorAuthRevoked:
        datastore.set_connection_status(
            org_id, source_id, "revoked", "source consent revoked"
        )
        datastore.audit(org_id, "worker", "sync_revoked",
                        {"source_id": source_id})
        return {"revoked": True}
    except ConnectorThrottled as e:
        datastore.audit(org_id, "worker", "sync_throttled",
                        {"source_id": source_id,
                         "retry_after": e.retry_after})
        raise ThrottledSync(e.retry_after) from e
