"""Pre-meeting context — a bounded, cited, READ-ONLY brief for one event.

Given (org, requesting user, calendar/meeting event) this assembles what a
person needs thirty seconds before a call: who the counterpart is, what
happened last time, what was promised and still isn't done, what the company
knows about the topic, and what is still unresolved.

Three properties are load-bearing and enforced structurally:

**Read-only.** This module performs no writes and calls nothing that does.
It never touches the action plane: no executor, no queue_action, no Pipedream,
no connected-app write path. Anything it suggests is a *discussion point* in
the pack; turning one into work stays the Action Center's job, behind explicit
approval. `PACK_IS_READ_ONLY` documents the invariant a test asserts.

**Permission-filtered before assembly, not after.** Meeting evidence comes
from `meeting_memory.search` and document evidence from the DF
`ContextResolver` — both take the AUTHENTICATED principal and apply DF's
single visibility query. A meeting or document the requester cannot read is
absent from the pack, including from its citations.

**Bounded.** Every section is capped. The whole point is to avoid handing a
meeting history to a model: the pack is small, cited evidence, and the
freshness stamp says exactly how current it is.

Never on the live transcript path — it does several DB round-trips, so
callers invoke it at dispatch/join time via run_in_threadpool.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

PACK_IS_READ_ONLY = True
PACK_VERSION = "premeeting@1"

_MAX_PRIOR_MEETINGS = 5
_MAX_COMMITMENTS = 8
_MAX_DOCS = 4
_RESOLVER_K = 12  # the ContextResolver's own ceiling
_MAX_RISKS = 6
_MAX_POINTS = 6
_LOOKBACK_DAYS = 180
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _attendees(event: dict) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for att in (event.get("attendees") or [])[:40]:
        name, email = "", ""
        if isinstance(att, str):
            found = _EMAIL.search(att)
            email = (found.group(0) if found else "").lower()
            name = att.replace(email, "").strip(" <>,") if email else att
        elif isinstance(att, dict):
            name = str(att.get("name") or att.get("displayName") or "")
            email = str(att.get("email") or att.get("mail") or "").lower()
        key = (email or name).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"name": name.strip(), "email": email})
    return out


def _query_for(event: dict, attendees: list[dict]) -> str:
    bits = [str(event.get("title") or ""), str(event.get("agenda") or "")]
    bits += [str(event.get("customer") or ""), str(event.get("project") or "")]
    bits += [a["name"] for a in attendees[:6] if a["name"]]
    return " ".join(b for b in bits if b).strip()


_STOP = {
    "the", "and", "for", "with", "our", "your", "this", "that", "call",
    "sync", "meeting", "weekly", "monthly", "review", "catch", "chat",
    "about", "from", "into", "next", "team", "update", "discussion",
}
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]{2,}")


def _document_queries(event: dict) -> list[str]:
    """Queries to try against company documents, most specific first.

    The document index is Postgres full text, and ``plainto_tsquery`` ANDs
    every term — so one long concatenated query ("Acme weekly sync rollout
    Ada") can never match anything. Instead: try the agenda/title phrase, then
    the salient individual terms. Bounded to a handful of lookups so a
    pre-meeting brief stays a brief, not a crawl.
    """
    candidates: list[str] = []
    agenda = " ".join(str(event.get("agenda") or "").split())
    title = " ".join(str(event.get("title") or "").split())
    if agenda:
        candidates.append(agenda)
    if title and title != agenda:
        candidates.append(title)
    seen: set[str] = set()
    terms: list[str] = []
    for source in (event.get("customer"), event.get("project"), agenda, title):
        for word in _WORD.findall(str(source or "")):
            low = word.lower()
            if low in _STOP or low in seen:
                continue
            seen.add(low)
            terms.append(word)
    candidates.extend(terms)
    out: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in out:
            out.append(candidate)
    return out[:5]


def build(
    org_id: str, *, principal_ref: str = "", event: Optional[dict] = None,
    avatar_key: str = "", now: Optional[float] = None,
) -> dict[str, Any]:
    """The context pack. `principal_ref` is the AUTHENTICATED requesting user;
    empty means no identity, which yields only org-visible evidence."""
    event = dict(event or {})
    now = float(now if now is not None else time.time())
    attendees = _attendees(event)
    query = _query_for(event, attendees)
    customer = str(event.get("customer") or "").strip()
    project = str(event.get("project") or "").strip()
    series_key = str(event.get("series_key") or "").strip()

    pack: dict[str, Any] = {
        "version": PACK_VERSION,
        "read_only": True,
        "event": {
            "title": str(event.get("title") or ""),
            "agenda": str(event.get("agenda") or "")[:2000],
            "starts_at": event.get("starts_at"),
            "organizer": str(event.get("organizer") or ""),
            "platform": str(event.get("platform") or ""),
        },
        "attendees": attendees,
        "relationship": {"customer": customer, "project": project,
                         "prior_meetings": 0, "last_met_at": None},
        "previously": [],
        "open_commitments": [],
        "company_knowledge": [],
        "risks_and_open_questions": [],
        "discussion_points": [],
        "citations": [],
        "freshness": {"generated_at": now, "evidence_window_days":
                      _LOOKBACK_DAYS, "degraded": False},
    }

    from . import enabled

    if not enabled():
        pack["freshness"]["degraded"] = True
        pack["freshness"]["reason"] = "data_foundation_disabled"
        return pack

    # ── prior meetings with these people / this customer / project ──
    meetings: list[dict] = []
    try:
        from . import meeting_memory

        filters: dict[str, Any] = {"since": now - _LOOKBACK_DAYS * 86400.0}
        if customer:
            filters["customer"] = customer
        if project:
            filters["project"] = project
        if series_key:
            filters["series_key"] = series_key
        found = meeting_memory.search(
            org_id, principal_ref=principal_ref, query=query,
            filters=filters, k=_MAX_PRIOR_MEETINGS * 2, include_body=True,
        )
        meetings = found.get("results") or []
        if not meetings and attendees:
            # No customer/project match — fall back to "meetings with this
            # person", which is the other way people remember a relationship.
            for person in attendees[:3]:
                who = person["email"] or person["name"]
                if not who:
                    continue
                by_person = meeting_memory.search(
                    org_id, principal_ref=principal_ref, query=query,
                    filters={"participant": who,
                             "since": now - _LOOKBACK_DAYS * 86400.0},
                    k=_MAX_PRIOR_MEETINGS, include_body=True,
                )
                meetings.extend(by_person.get("results") or [])
                if meetings:
                    break
        if found.get("resolution", {}).get("group_resolution_incomplete"):
            pack["freshness"]["degraded"] = True
            pack["freshness"]["reason"] = "group_resolution_incomplete"
    except Exception as exc:  # noqa: BLE001 — degrade, never widen, never 500
        print(f"[premeeting] meeting evidence degraded: {type(exc).__name__}",
              flush=True)
        pack["freshness"]["degraded"] = True

    seen_meetings: set[str] = set()
    for hit in meetings:
        cite = hit.get("citation") or {}
        mid = str(cite.get("meeting_id") or "")
        if not mid or mid in seen_meetings:
            continue
        seen_meetings.add(mid)
        if len(pack["previously"]) < _MAX_PRIOR_MEETINGS:
            pack["previously"].append({
                "meeting_id": mid,
                "title": cite.get("title") or "",
                "date": cite.get("date"),
                "excerpt": str(hit.get("excerpt") or "")[:600],
            })
            pack["citations"].append({
                "kind": "meeting", "meeting_id": mid,
                "title": cite.get("title") or "", "date": cite.get("date"),
                "url": cite.get("canonical_url") or "",
            })
        _harvest(hit, pack)
        last = cite.get("date")
        if last and (pack["relationship"]["last_met_at"] is None
                     or float(last) > float(pack["relationship"]["last_met_at"])):
            pack["relationship"]["last_met_at"] = float(last)
        if not pack["relationship"]["customer"] and cite.get("customer"):
            pack["relationship"]["customer"] = cite["customer"]
        if not pack["relationship"]["project"] and cite.get("project"):
            pack["relationship"]["project"] = cite["project"]
    pack["relationship"]["prior_meetings"] = len(seen_meetings)

    # ── company knowledge (documents), through the ONE resolver boundary ──
    seen_records: set[str] = set()
    for doc_query in _document_queries(event):
        if len(pack["company_knowledge"]) >= _MAX_DOCS:
            break
        try:
            from . import resolver

            # Ask for more candidates than we keep: meeting bodies live in
            # the same chunk store and are filtered out below, so a small k
            # would let them crowd every document off the list.
            resolved = resolver.resolve(
                org_id, avatar_key or "laura", doc_query, k=_RESOLVER_K,
                principal_id=principal_ref, purpose="premeeting",
            )
        except Exception as exc:  # noqa: BLE001 — degrade, never widen
            print(f"[premeeting] document evidence degraded: "
                  f"{type(exc).__name__}", flush=True)
            pack["freshness"]["degraded"] = True
            break
        if resolved.get("resolution", {}).get("degraded"):
            pack["freshness"]["degraded"] = True
        for chunk in resolved.get("chunks") or []:
            record_id = str(chunk.get("record_id") or "")
            cite_kind = str((chunk.get("citation") or {}).get("connector_kind")
                            or "")
            if not record_id or record_id in seen_records:
                # No record id = a base-pack chunk, which carries no citable
                # company record; duplicates come from the query fan-out.
                continue
            if cite_kind == "meeting":
                # Meeting bodies are materialized as knowledge documents, so
                # they surface here too — but they are already the
                # "previously" section. Company knowledge means documents.
                continue
            if len(pack["company_knowledge"]) >= _MAX_DOCS:
                break
            seen_records.add(record_id)
            cite = chunk.get("citation") or {}
            pack["company_knowledge"].append({
                "excerpt": str(chunk.get("text") or "")[:600],
                "source": cite.get("source_name") or "",
                "section": cite.get("section") or "",
                "url": cite.get("canonical_url") or "",
            })
            pack["citations"].append({
                "kind": "document",
                "title": cite.get("source_name") or "",
                "url": cite.get("canonical_url") or "",
                "record_id": record_id,
            })

    pack["discussion_points"] = _points(pack)
    return pack


_ACTION_LINE = re.compile(r"^-\s+(.*)$", re.MULTILINE)


def _section(body: str, heading: str) -> str:
    """Just the named section of a distilled meeting — up to the NEXT '##'.
    Splitting on the heading alone would swallow every following section, so
    risks would be harvested as commitments."""
    if heading not in body:
        return ""
    rest = body.split(heading, 1)[1]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _harvest(hit: dict, pack: dict) -> None:
    """Pull open commitments and risks out of a distilled meeting excerpt.

    The distilled body has stable '## Actions' / '## Risks' / '## Open
    questions' sections, so this is parsing our own format, not guessing at
    free text. Prefers the full record over the query excerpt — the excerpt is
    whichever section matched the query, which usually is not the one holding
    the commitments. Every harvested line keeps the meeting id that justifies
    it: an uncited commitment never enters the pack."""
    body = str(hit.get("body") or hit.get("excerpt") or "")
    cite = hit.get("citation") or {}
    mid = str(cite.get("meeting_id") or "")
    seen_risks = {item["text"] for item in pack["risks_and_open_questions"]}
    for line in _ACTION_LINE.findall(_section(body, "## Actions")):
        if len(pack["open_commitments"]) >= _MAX_COMMITMENTS:
            break
        owner, due = "", ""
        parts = [p.strip() for p in line.split("—")]
        title = parts[0]
        for part in parts[1:]:
            if part.lower().startswith("owner:"):
                owner = part.split(":", 1)[1].strip()
            elif part.lower().startswith("due:"):
                due = part.split(":", 1)[1].strip()
        pack["open_commitments"].append({
            "title": title[:300], "owner": owner[:120], "due": due[:60],
            "from_meeting_id": mid,
        })
    for heading, kind in (("## Risks", "risk"),
                          ("## Open questions", "open_question")):
        for line in _ACTION_LINE.findall(_section(body, heading)):
            if len(pack["risks_and_open_questions"]) >= _MAX_RISKS:
                break
            text = line.strip()[:300]
            if text in seen_risks:
                continue  # the same risk carried across meetings, listed once
            seen_risks.add(text)
            pack["risks_and_open_questions"].append({
                "text": text, "from_meeting_id": mid, "kind": kind,
            })


def _points(pack: dict) -> list[dict[str, str]]:
    """Suggested discussion points, each traceable to evidence already in the
    pack. Suggestions only — nothing here schedules, sends, or executes."""
    points: list[dict[str, str]] = []
    for commitment in pack["open_commitments"][:_MAX_POINTS]:
        owner = commitment.get("owner") or "the owner"
        points.append({
            "point": f"Confirm status: {commitment['title']} ({owner})",
            "because": "open commitment from a previous meeting",
            "from_meeting_id": commitment.get("from_meeting_id", ""),
        })
    for item in pack["risks_and_open_questions"]:
        if len(points) >= _MAX_POINTS:
            break
        points.append({
            "point": f"Revisit: {item['text']}",
            "because": f"unresolved {item['kind'].replace('_', ' ')}",
            "from_meeting_id": item.get("from_meeting_id", ""),
        })
    if not points and pack["previously"]:
        points.append({
            "point": f"Pick up from: {pack['previously'][0]['title']}",
            "because": "most recent related meeting",
            "from_meeting_id": pack["previously"][0]["meeting_id"],
        })
    return points[:_MAX_POINTS]
