"""ContextResolver (accepted contract v5) — the ONE retrieval boundary.

Effective visibility = tenant ∩ connector eligibility ∩ authenticated-
principal ACL ∩ published avatar scope ∩ request scope ∩ non-tombstoned
current versions ∩ retention. The live-meeting guarantee is structural: the
per-org index only ever contains org_default content, so this module is the
sole gateway to principal-scoped mirrored records — and it is never on the
transcript path.

Degradation NEVER widens: any DF failure returns exactly what the M2 path
returns (the resolved avatar's own mask), zero DF additions, degraded=true.
acl_filtered_count exists only behind the admin debug endpoint. This module
does not create a second retrieval subsystem: semantic ranking stays
rag.retrieve; DF adds an ACL'd keyword layer over the SAME chunk store.
"""
from __future__ import annotations

from typing import Any, Optional

from .. import avatar_resolver, rag
from . import dal


def _m2_chunks(org_id: str, avatar_key: str, query: str, k: int) -> list[dict]:
    """Exactly what the M2 seam yields today — the floor AND the ceiling of
    any degraded response."""
    resolved = avatar_resolver.resolve(org_id, avatar_key)
    hits = rag.retrieve(resolved, query, k=k, org_id=org_id)
    return [
        {"text": h.text[:800], "score": round(float(h.score), 4),
         "citation": {"source_name": h.source, "section": h.section,
                      "connector_kind": "", "canonical_url": "",
                      "external_updated_at": None},
         "record_id": None, "version_id": None, "lineage": None}
        for h in hits
    ]


def resolve(
    org_id: str, avatar_key: str, query: str, *, k: int = 6,
    principal_id: str = "", purpose: str = "",
    scope: Optional[dict] = None,
) -> dict[str, Any]:
    """handshake operation: df-context-resolver. org_id/principal_id come
    from the AUTHENTICATED caller only (the router enforces it)."""
    k = max(1, min(int(k or 6), 12))
    base = _m2_chunks(org_id, avatar_key, query, k)
    from . import enabled

    if not enabled():
        return {
            "chunks": base, "applied_scope": scope or {},
            "freshness": {"oldest_ingested_at": None,
                          "newest_ingested_at": None,
                          "stale_connectors": [],
                          "tombstone_lag_seconds": 0.0},
            "resolution": {"complete": True, "degraded": False,
                           "group_resolution_incomplete": False},
        }
    try:
        identity_ids: set[str] = set()
        group_complete = True
        if principal_id:
            identity_ids, group_complete = dal.principal_identity_closure(
                org_id, principal_id
            )
        df_scope = scope if isinstance(scope, dict) else {}
        heads = dal.visible_heads(
            org_id,
            principal_identity_ids=identity_ids,
            connector_ids=df_scope.get("connector_ids") or None,
            container_external_ids=(
                df_scope.get("container_external_ids") or None
            ),
            kinds=df_scope.get("kinds") or None,
        )
        df_chunks = _df_keyword_chunks(org_id, query, heads, k)
        merged = _merge(base, df_chunks, k)
        return {
            "chunks": merged,
            "applied_scope": df_scope,
            "freshness": dal.freshness(org_id),
            "resolution": {
                "complete": group_complete,
                "degraded": False,
                "group_resolution_incomplete": not group_complete,
            },
        }
    except Exception as exc:  # noqa: BLE001 — degrade, never widen, never 500
        # Class + driver message only (SQL/driver text, never record content).
        print(f"[df-resolver] degraded: {type(exc).__name__}: "
              f"{str(exc)[:200]}", flush=True)
        return {
            "chunks": base, "applied_scope": scope or {},
            "freshness": {"oldest_ingested_at": None,
                          "newest_ingested_at": None,
                          "stale_connectors": [],
                          "tombstone_lag_seconds": 0.0},
            "resolution": {"complete": False, "degraded": True,
                           "group_resolution_incomplete": False},
        }


def _df_keyword_chunks(org_id: str, query: str, heads: list[dict],
                       k: int) -> list[dict]:
    """ACL'd DF layer over the SAME knowledge chunk store: keyword search
    bounded to the visible heads' body documents, cited with lineage.
    Records without bodies contribute title-level hits."""
    by_doc: dict[str, dict] = {}
    for head in heads:
        parts = str(head.get("body_ref") or "").split(":")
        if len(parts) == 3 and parts[0] == "kdv":
            by_doc[parts[2]] = head
    out: list[dict] = []
    if by_doc:
        from ..knowledge import dal as kdal

        hits = kdal.keyword_search(org_id, query, limit=k * 2)
        for hit in hits:
            head = by_doc.get(str(hit.get("document_id") or ""))
            if head is None:
                continue  # outside the visible set — ACL filter holds
            out.append({
                "text": str(hit["text"])[:800],
                "score": round(float(hit.get("rank") or 0.0), 4),
                "citation": {
                    "source_name": str(hit.get("source") or head["title"]),
                    "section": str(hit.get("section") or ""),
                    "connector_kind": str(head.get("connector_kind") or ""),
                    "canonical_url": str(head.get("canonical_url") or ""),
                    "external_updated_at": head.get("external_updated_at"),
                },
                "record_id": head["id"],
                "version_id": head["version_id"],
                "lineage": head.get("lineage_json"),
            })
    # Title-level matches for structured/no-body records.
    terms = {t for t in query.lower().split() if len(t) > 2}
    for head in heads:
        if head.get("body_ref"):
            continue
        title = str(head.get("title") or "").lower()
        if terms and any(t in title for t in terms):
            out.append({
                "text": str(head.get("title") or "")[:300],
                "score": 0.2,
                "citation": {
                    "source_name": str(head.get("title") or ""),
                    "section": "",
                    "connector_kind": str(head.get("connector_kind") or ""),
                    "canonical_url": str(head.get("canonical_url") or ""),
                    "external_updated_at": head.get("external_updated_at"),
                },
                "record_id": head["id"],
                "version_id": head["version_id"],
                "lineage": head.get("lineage_json"),
            })
    return out[: k * 2]


def _merge(base: list[dict], df_chunks: list[dict], k: int) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    for chunk in sorted(base + df_chunks, key=lambda c: -c["score"]):
        key = (chunk["text"][:120]).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(chunk)
        if len(merged) >= k:
            break
    return merged


def acl_stats(org_id: str) -> dict[str, int]:
    """ADMIN/DEBUG ONLY — never in ordinary resolver responses."""
    from sqlalchemy import text

    from .. import control_plane

    engine = control_plane._get_engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT
                  count(*) FILTER (WHERE NOT r.tombstoned) AS live,
                  count(*) FILTER (
                    WHERE NOT r.tombstoned AND r.acl_mode='unknown'
                  ) AS unknown_acl,
                  count(*) FILTER (
                    WHERE NOT r.tombstoned AND r.acl_mode='mirrored'
                  ) AS mirrored,
                  count(*) FILTER (
                    WHERE NOT r.tombstoned AND r.acl_mode='org_default'
                  ) AS org_default
                FROM df_source_records r
                WHERE r.org_id=:org_id
                """
            ),
            {"org_id": org_id},
        ).mappings().one()
    stats = {key: int(value) for key, value in row.items()}
    stats["acl_filtered_count"] = stats["live"] - stats["org_default"]
    return stats
