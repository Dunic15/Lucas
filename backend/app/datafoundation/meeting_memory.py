"""Meeting Memory retrieval — permission-filtered search across past meetings.

Answers questions that span many meetings ("what did we decide with Acme in
the last six months", "which commitments to Sarah are still open", "recurring
risks on this project") WITHOUT ever handing a meeting history to a model:
the caller gets a bounded, ranked, cited evidence set and nothing else.

Authorization order is the whole point and never changes:

  1. ``dal.visible_heads`` produces the ACL-filtered head set for the
     AUTHENTICATED principal (org ∩ connector eligibility ∩ principal ACL ∩
     non-tombstoned ∩ eligible connector). This is DF's single visibility
     query — this module does not re-implement it and cannot widen it.
  2. Facet filters (participant / customer / project / topic / date range)
     are applied ON TOP of that set, so they only ever NARROW.
  3. Only then is text scored, and only for records that survived both.

A caller with no resolvable principal gets an empty head set, so the default
is deny, not "everything the org can see". An inaccessible meeting therefore
contributes no snippet and no citation — there is no code path where it can.

Citations carry meeting title, date and meeting id, per the M3 contract.
"""
from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Optional

from sqlalchemy import text

from .. import control_plane
from . import dal

_MAX_HEADS = 400
_RECENCY_HALF_LIFE_DAYS = 120.0
_EXCERPT_CHARS = 700


def _engine():
    return control_plane._get_engine()


# ── facet projection (written at index time) ────────────────────────────────

def replace_facets(org_id: str, record_id: str, fields: dict[str, Any],
                   *, series_key: str = "") -> None:
    """Project one distilled meeting into the filterable facet row."""
    participants = [
        {"name": p.get("name", ""), "email": (p.get("email") or "").lower()}
        for p in (fields.get("participants") or [])
    ]
    open_actions = len([
        a for a in (fields.get("actions") or [])
        if not str(a.get("status") or "").lower().startswith("done")
    ])
    occurred = str(fields.get("date") or "") or None
    engine = _engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org_id)
        conn.execute(
            text(
                """
                INSERT INTO df_meeting_facets (
                  org_id, record_id, meeting_id, title, platform, customer,
                  project, series_key, occurred_at, participants_json,
                  topics_json, open_actions, indexed_at
                ) VALUES (
                  :org_id, CAST(:record_id AS uuid), :meeting_id, :title,
                  :platform, :customer, :project, :series_key,
                  NULLIF(:occurred, '')::timestamptz, :participants,
                  :topics, :open_actions, clock_timestamp()
                )
                ON CONFLICT (org_id, record_id) DO UPDATE SET
                  meeting_id=excluded.meeting_id, title=excluded.title,
                  platform=excluded.platform, customer=excluded.customer,
                  project=excluded.project, series_key=excluded.series_key,
                  occurred_at=excluded.occurred_at,
                  participants_json=excluded.participants_json,
                  topics_json=excluded.topics_json,
                  open_actions=excluded.open_actions,
                  indexed_at=clock_timestamp()
                """
            ),
            {
                "org_id": org_id, "record_id": record_id,
                "meeting_id": str(fields.get("meeting_id") or "")[:200],
                "title": str(fields.get("title") or "")[:300],
                "platform": str(fields.get("platform") or "")[:40],
                "customer": str(fields.get("customer") or "")[:200],
                "project": str(fields.get("project") or "")[:200],
                "series_key": str(series_key or "")[:200],
                "occurred": occurred or "",
                "participants": json.dumps(participants)[:4000],
                "topics": json.dumps(fields.get("topics") or [])[:2000],
                "open_actions": int(open_actions),
            },
        )


def _facets_for(org_id: str, record_ids: list[str]) -> dict[str, dict]:
    if not record_ids:
        return {}
    engine = _engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT record_id::text, meeting_id, title, platform, customer,
                       project, series_key, participants_json, topics_json,
                       open_actions,
                       extract(epoch from occurred_at)::float8 AS occurred_at
                FROM df_meeting_facets
                WHERE org_id=:org_id
                  AND record_id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            {"org_id": org_id, "ids": list(record_ids)},
        ).mappings().all()
    out: dict[str, dict] = {}
    for row in rows:
        facet = dict(row)
        for key, default in (("participants_json", "[]"),
                             ("topics_json", "[]")):
            try:
                facet[key[:-5]] = json.loads(facet.pop(key) or default)
            except ValueError:
                facet[key[:-5]] = []
        out[str(row["record_id"])] = facet
    return out


# ── query ───────────────────────────────────────────────────────────────────

def _matches(facet: dict, filters: dict) -> bool:
    """Facet filters NARROW an already-ACL-filtered head. Absent facets fail
    every positive filter (a meeting we cannot describe is not a match)."""
    def norm(value: Any) -> str:
        return " ".join(str(value or "").split()).lower()

    customer = norm(filters.get("customer"))
    if customer and customer not in norm(facet.get("customer")):
        return False
    project = norm(filters.get("project"))
    if project and project not in norm(facet.get("project")):
        return False
    topic = norm(filters.get("topic"))
    if topic:
        topics = [norm(t) for t in facet.get("topics") or []]
        title = norm(facet.get("title"))
        if not any(topic in t for t in topics) and topic not in title:
            return False
    participant = norm(filters.get("participant"))
    if participant:
        people = facet.get("participants") or []
        hit = any(
            participant in norm(p.get("name"))
            or participant in norm(p.get("email"))
            for p in people
        )
        if not hit:
            return False
    series = norm(filters.get("series_key"))
    if series and series != norm(facet.get("series_key")):
        return False
    occurred = facet.get("occurred_at")
    since = filters.get("since")
    until = filters.get("until")
    if since is not None and (occurred is None or occurred < float(since)):
        return False
    if until is not None and (occurred is None or occurred > float(until)):
        return False
    return True


def _score(text_blob: str, terms: set[str]) -> float:
    if not terms:
        return 0.0
    blob = text_blob.lower()
    hits = sum(1 for term in terms if term in blob)
    return hits / float(len(terms))


def _recency(occurred_at: Optional[float]) -> float:
    if not occurred_at:
        return 0.0
    age_days = max(0.0, (time.time() - float(occurred_at)) / 86400.0)
    return 0.15 * math.pow(0.5, age_days / _RECENCY_HALF_LIFE_DAYS)


_TERM = re.compile(r"[a-z0-9][a-z0-9'-]{1,}")


def search(
    org_id: str, *, principal_ref: str = "", query: str = "",
    filters: Optional[dict] = None, k: int = 8,
    include_body: bool = False,
) -> dict[str, Any]:
    """Bounded, cited, permission-filtered meeting evidence.

    principal_ref is the AUTHENTICATED caller. Empty means no principal, which
    yields only org-visible meetings — never everything.

    ``include_body`` adds the full distilled record (structured sections) to
    each hit. It does NOT widen anything: results are already ACL-filtered, so
    this only returns more of a meeting the caller may already read. The
    pre-meeting brief uses it to harvest open actions and risks, which live in
    sections the query excerpt may not have selected.
    """
    filters = dict(filters or {})
    k = max(1, min(int(k or 8), 20))
    identity_ids: set[str] = set()
    group_complete = True
    if principal_ref:
        identity_ids, group_complete = dal.principal_identity_closure(
            org_id, principal_ref
        )
    heads = dal.visible_heads(
        org_id,
        principal_identity_ids=identity_ids,
        kinds=["event"],
        container_external_ids=(
            [filters["series_key"]] if filters.get("series_key") else None
        ),
        limit=_MAX_HEADS,
    )
    heads = [h for h in heads if str(h.get("connector_kind")) == "meeting"]
    facets = _facets_for(org_id, [str(h["id"]) for h in heads])
    kept = [h for h in heads if _matches(facets.get(str(h["id"]), {}), filters)]
    if not kept:
        return {"results": [], "meetings": 0,
                "resolution": {"complete": group_complete,
                               "group_resolution_incomplete": not group_complete}}

    terms = {t for t in _TERM.findall(str(query or "").lower()) if len(t) > 2}
    bodies = _bodies_for(org_id, kept)
    scored: list[tuple[float, dict]] = []
    for head in kept:
        facet = facets.get(str(head["id"]), {})
        body = bodies.get(str(head["id"]), "")
        base = _score(f"{head.get('title') or ''}\n{body}", terms)
        if terms and base <= 0.0:
            continue
        score = base + _recency(facet.get("occurred_at"))
        hit: dict[str, Any] = {
            "excerpt": _excerpt(body, terms) or str(head.get("title") or ""),
            "score": round(float(score), 4),
            "citation": {
                "meeting_id": facet.get("meeting_id")
                or str(head.get("external_id") or ""),
                "title": facet.get("title") or str(head.get("title") or ""),
                "date": facet.get("occurred_at"),
                "platform": facet.get("platform") or "",
                "canonical_url": str(head.get("canonical_url") or ""),
                "customer": facet.get("customer") or "",
                "project": facet.get("project") or "",
            },
            "record_id": head["id"],
            "version_id": head["version_id"],
        }
        if include_body:
            hit["body"] = body
        scored.append((score, hit))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["citation"]["meeting_id"]))
    results = [item for _, item in scored[:k]]
    return {
        "results": results,
        "meetings": len({r["citation"]["meeting_id"] for r in results}),
        "resolution": {"complete": group_complete,
                       "group_resolution_incomplete": not group_complete},
    }


def _bodies_for(org_id: str, heads: list[dict]) -> dict[str, str]:
    """Distilled body text for visible meeting heads, read through the same
    body_ref linkage DF already uses (kdv:<version_id>:<document_id>)."""
    by_version: dict[str, str] = {}
    for head in heads:
        parts = str(head.get("body_ref") or "").split(":")
        if len(parts) == 3 and parts[0] == "kdv":
            by_version[parts[1]] = str(head["id"])
    if not by_version:
        return {}
    engine = _engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT id::text, text_content
                FROM knowledge_document_versions
                WHERE org_id=:org_id AND id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            {"org_id": org_id, "ids": list(by_version)},
        ).mappings().all()
    return {by_version[str(r["id"])]: str(r["text_content"] or "")
            for r in rows if str(r["id"]) in by_version}


def _excerpt(body: str, terms: set[str]) -> str:
    """The most query-relevant section of the distilled meeting, so a cited
    answer quotes the decision/action/risk rather than the whole record."""
    if not body:
        return ""
    sections = [s.strip() for s in body.split("\n\n") if s.strip()]
    if not terms:
        return "\n\n".join(sections[:2])[:_EXCERPT_CHARS]
    best, best_score = "", -1.0
    for section in sections:
        score = _score(section, terms)
        if score > best_score:
            best, best_score = section, score
    return best[:_EXCERPT_CHARS] if best_score > 0 else sections[0][:_EXCERPT_CHARS]
