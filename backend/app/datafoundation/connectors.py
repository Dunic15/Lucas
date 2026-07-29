"""DF connectors (accepted contract v5): upload + network-free fake Drive.

- ``upload`` wraps the M1 Company Brain: publishes emit org_default
  envelopes at ingest time (hook in knowledge/ingest.py) and ``full`` sync is
  the backfill over already-published documents. acl_mode is EXPLICITLY
  org_default — the only connector allowed to say so by default.
- ``gdrive`` here is the contract-mandated NETWORK-FREE fake: it speaks the
  real Drive *Changes page token* protocol (start token, monotonically
  increasing change entries, token-expiry -> full reconciliation) against a
  fixture held in the connector's config — deterministic for tests, seeds,
  and the Control Center's connector/ACL/freshness states. The real HTTP
  client is credential-gated future work behind the same protocol; watermark
  incrementals are forbidden by contract either way.
- All other kinds raise ConnectorNotImplemented (legal rows, failed runs).

Connectors NEVER touch the database directly — they return envelope batches
+ identities + the new cursor; dal.commit_batch applies everything
atomically with the cursor advance.
"""
from __future__ import annotations

from typing import Any

from . import dal
from .envelope import body_checksum


class ConnectorNotImplemented(RuntimeError):
    pass


class SyncBatch:
    def __init__(self, envelopes: list[dict], *, identities: list[dict]
                 | None = None, new_cursor: dict | None = None):
        self.envelopes = envelopes
        self.identities = identities or []
        self.new_cursor = new_cursor


class UploadConnector:
    kind = "upload"

    def sync(self, org_id: str, connector: dict, cursor: dict,
             *, full: bool) -> SyncBatch:
        if not full:
            # Event-driven: publishes emit envelopes at ingest time; an
            # incremental poll has nothing to do.
            return SyncBatch([], new_cursor=cursor or {"mode": "event"})
        return SyncBatch(
            self.backfill_envelopes(org_id),
            new_cursor={"mode": "event", "backfilled": True},
        )

    @staticmethod
    def backfill_envelopes(org_id: str) -> list[dict]:
        """Envelopes for every already-published Company Brain document —
        the binding rollout step 'upload backfill'."""
        from .. import knowledge
        from ..knowledge import dal as kdal

        if not knowledge.enabled():
            return []
        out: list[dict] = []
        for source in kdal.list_sources(org_id):
            for doc in kdal.list_documents(org_id, source["id"]):
                if doc.get("status") == "deleted":
                    continue
                env = envelope_for_document(org_id, source, doc)
                if env is not None:
                    out.append(env)
        return out


def envelope_for_document(org_id: str, source: dict, doc: dict) -> dict | None:
    """The upload connector's SourceEnvelope for one published knowledge
    document (also used by the publish-time hook)."""
    from ..knowledge import dal as kdal

    version_no = int(doc.get("latest_version") or 0)
    if version_no <= 0:
        return None
    # Resolve the version id + checksum for the body_ref linkage.
    from sqlalchemy import text

    from .. import control_plane

    engine = control_plane._get_engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT id::text, checksum FROM knowledge_document_versions
                WHERE org_id=:org_id AND document_id=CAST(:doc AS uuid)
                ORDER BY version DESC LIMIT 1
                """
            ),
            {"org_id": org_id, "doc": doc["id"]},
        ).mappings().first()
    if row is None:
        return None
    _ = kdal  # imported for parity; listing already happened upstream
    return {
        "external_id": str(doc["id"]),
        "kind": "document",
        "title": str(doc.get("filename") or ""),
        "mime": str(doc.get("mime") or ""),
        "canonical_url": "",
        "container_external_id": str(source["id"]),
        "external_updated_at": "",
        "acl_mode": "org_default",  # upload says it EXPLICITLY (contract)
        "deleted": False,
        "checksum": str(row["checksum"] or ""),
        "body_ref": f"kdv:{row['id']}:{doc['id']}",
        "transform": "m1-ingest@1",
    }


class FakeGDriveConnector:
    """Drive Changes-token protocol over a fixture — zero network.

    Fixture shape (connector.config_json['fixture']):
      {"files": {fid: {title, body, container, author, acl_mode,
                       acl: [...], permissions_hidden?: bool}},
       "changes": [{"token": 3, "file": fid, "op": "upsert|delete|acl",
                    ...file overrides}],
       "identities": [{external_id, kind, display, email, email_verified,
                       members?}],
       "start_token": 1, "latest_token": N,
       "scope_lost": false, "expire_tokens_below": 0}
    """

    kind = "gdrive"

    def sync(self, org_id: str, connector: dict, cursor: dict,
             *, full: bool) -> SyncBatch:
        fixture = (connector.get("config_json") or {}).get("fixture")
        if not isinstance(fixture, dict):
            raise ConnectorNotImplemented(
                "gdrive network client is credential-gated and deferred; "
                "this connector runs only with a network-free fixture"
            )
        if fixture.get("scope_lost"):
            raise dal.ScopeLostError("drive permission scope lost")
        latest = int(fixture.get("latest_token") or 0)
        token = int((cursor or {}).get("page_token") or 0)
        expired_below = int(fixture.get("expire_tokens_below") or 0)
        identities = list(fixture.get("identities") or [])
        if full or token <= 0 or token < expired_below:
            # Full reconciliation (the contract's fallback when no valid
            # Changes page token exists — never a modified-time watermark).
            envelopes = [
                self._file_envelope(fid, meta)
                for fid, meta in (fixture.get("files") or {}).items()
            ]
            return SyncBatch(envelopes, identities=identities,
                            new_cursor={"page_token": latest})
        changes = [
            c for c in (fixture.get("changes") or [])
            if int(c.get("token") or 0) > token
        ]
        envelopes = []
        files = fixture.get("files") or {}
        for change in sorted(changes, key=lambda c: int(c.get("token") or 0)):
            fid = str(change.get("file") or "")
            base = dict(files.get(fid) or {})
            base.update({k: v for k, v in change.items()
                         if k not in ("token", "file", "op")})
            if change.get("op") == "delete":
                envelopes.append({
                    "external_id": fid, "kind": "document",
                    "title": str(base.get("title") or fid),
                    "external_updated_at": "",
                    "acl_mode": str(base.get("acl_mode") or "unknown"),
                    "acl": base.get("acl") or [],
                    "deleted": True,
                    "checksum": body_checksum(str(base.get("body") or "")),
                })
            else:
                envelopes.append(self._file_envelope(fid, base))
        return SyncBatch(envelopes, identities=identities,
                        new_cursor={"page_token": latest})

    @staticmethod
    def _file_envelope(fid: str, meta: dict) -> dict:
        acl_mode = str(meta.get("acl_mode") or "")
        if meta.get("permissions_hidden"):
            acl_mode = "unknown"  # fail-closed: permissions unreadable
        body = str(meta.get("body") or "")
        return {
            "external_id": str(fid),
            "kind": "document",
            "title": str(meta.get("title") or fid),
            "body_text": body,
            "mime": str(meta.get("mime") or "text/plain"),
            "canonical_url": f"https://drive.fake/{fid}",
            "author_external_id": str(meta.get("author") or ""),
            "container_external_id": str(meta.get("container") or ""),
            "external_updated_at": str(meta.get("updated_at") or ""),
            "acl_mode": acl_mode or "unknown",
            "acl": meta.get("acl") or [],
            "deleted": False,
            "checksum": body_checksum(body),
            "transform": "fake-gdrive@1",
        }


def _msgraph():
    # Imported lazily: connector_msgraph imports this module for SyncBatch /
    # ConnectorNotImplemented, so a top-level import would be circular.
    from .connector_msgraph import MSGraphConnector

    return MSGraphConnector()


def _meeting():
    from .connector_meeting import MeetingConnector

    return MeetingConnector()


_REGISTRY: dict[str, Any] = {
    "upload": UploadConnector(),
    "gdrive": FakeGDriveConnector(),
}

_LAZY: dict[str, Any] = {
    "msgraph": _msgraph,
    "meeting": _meeting,
}


def get(kind: str):
    key = str(kind or "")
    connector = _REGISTRY.get(key)
    if connector is None and key in _LAZY:
        connector = _LAZY[key]()
        _REGISTRY[key] = connector
    if connector is None:
        raise ConnectorNotImplemented(f"connector kind {kind!r} is deferred")
    return connector
