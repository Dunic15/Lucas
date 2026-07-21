"""Northstar knowledge ingestion; through the CANONICAL Company Brain / Data
Foundation path, never a direct table insert.

Loads ``demos/northstar/knowledge/*.md`` into ONE demo org using exactly the
seams a real upload uses: create_source(upload) → upsert_document →
storage.put_bytes → set_document_storage → ingest.ingest_document (publishes an
immutable version with an extracted-text checksum, and; when Data Foundation
is on; mirrors a DF SourceEnvelope head at acl_mode=org_default) → assign to
the demo avatar → rebuild the per-org index. Idempotent: re-running dedupes by
filename + extracted-text checksum and re-assigns without duplicating.

The reset path clears ONLY the demo org's knowledge (never another org's).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .. import control_plane
from . import manifest


def _require_brain() -> None:
    from .. import knowledge

    if not knowledge.enabled():
        raise RuntimeError(
            "Company Brain is off (COMPANY_BRAIN_ENABLED + a control-plane "
            "Postgres are required to ingest Northstar knowledge)")


# Every ingested demo source is namespaced with this prefix so reset() can
# match on DEMO PROVENANCE, never on a bare human-readable slug that could
# collide with a REAL org source (e.g. "security-policy"/"approval-policy").
_PREFIX = "northstar-"


def _source_name(slug: str) -> str:
    # e.g. security_policy.md -> "northstar-security-policy" (idempotent prefix).
    base = slug.replace("_", "-")
    return base if base.startswith(_PREFIX) else _PREFIX + base


def setup(org_id: str, *, avatar_key: str = "laura") -> dict[str, Any]:
    """Idempotently ingest the whole Northstar knowledge pack into ``org_id``
    and assign it to ``avatar_key``. Returns a summary."""
    _require_brain()
    from ..knowledge import dal as kdal
    from ..knowledge import ingest as kingest
    from ..knowledge import storage as kstorage

    kdir = manifest.knowledge_dir()
    files = sorted(p for p in kdir.glob("*.md"))
    ingested: list[str] = []
    for path in files:
        source_name = _source_name(path.stem)
        # One upload source per document (idempotent create).
        existing = next((s for s in kdal.list_sources(org_id)
                         if s.get("name") == source_name), None)
        source = existing or kdal.create_source(org_id, source_name, "upload")
        if source is None:
            continue
        source_id = source["id"]
        data = path.read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        doc = kdal.upsert_document(org_id, source_id, path.name,
                                   mime="text/markdown", size_bytes=len(data),
                                   checksum=checksum)
        if doc is None:
            continue
        ref = kstorage.put_bytes(org_id, doc["id"], path.name, data)
        kdal.set_document_storage(org_id, doc["id"], ref)
        # Publish the immutable version (idempotent on extracted-text checksum).
        kingest.ingest_document(org_id, doc["id"])
        kdal.assign(org_id, source_id, avatar_key)
        ingested.append(source_name)
    # Make the published + assigned chunks retrievable now.
    kingest.rebuild_indexes(org_id)
    return {"org_id": org_id, "avatar_key": avatar_key,
            "sources": ingested, "count": len(ingested)}


def reset(org_id: str) -> dict[str, Any]:
    """Remove ONLY this org's DEMO knowledge sources; matched strictly on the
    ``northstar-`` provenance prefix, never on a bare generic slug. A real org
    source (e.g. its own "security-policy") is therefore never touched. Deletes
    are org-scoped (RLS) so another org is never affected."""
    _require_brain()
    from ..knowledge import dal as kdal
    from ..knowledge import ingest as kingest

    removed = 0
    for source in kdal.list_sources(org_id):
        if str(source.get("name", "")).startswith(_PREFIX):
            if kdal.delete_source(org_id, source["id"]):
                removed += 1
    kingest.rebuild_indexes(org_id)
    return {"org_id": org_id, "removed": removed}


def verify_retrieval(org_id: str, *, avatar_key: str = "laura") -> dict[str, Any]:
    """Prove ContextResolver returns Northstar/Acme citations for this org."""
    from ..datafoundation import resolver

    m = manifest.load()
    checks = []
    for probe in (m.get("expected_citations") or []):
        result = resolver.resolve(
            org_id, avatar_key, probe["heading"], k=6,
            purpose="northstar-demo-verify")
        chunks = result.get("chunks") or []
        found = any(
            probe["source_id"] in str(c.get("citation", {}).get("source_name", ""))
            or probe["heading"].lower() in str(c.get("text", "")).lower()
            for c in chunks)
        checks.append({"source_id": probe["source_id"],
                       "heading": probe["heading"], "found": found,
                       "chunks": len(chunks)})
    return {"org_id": org_id, "checks": checks,
            "all_found": all(c["found"] for c in checks) if checks else False}


def provision_demo_org(email: str = "northstar-demo@northstar.test") -> str:
    """Idempotently provision (or resolve) the demo org via the canonical
    key-free provisioner. Returns its org_id."""
    if not control_plane.enabled():
        raise RuntimeError("control plane (LAURA_DATABASE_URL) required")
    result = control_plane.ensure_user("", email, "Northstar Demo")
    if result is None:
        raise RuntimeError("could not provision the demo org")
    return result["org_id"]
