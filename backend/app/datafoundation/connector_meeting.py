"""Meeting Memory — native Laura meetings as a first-party DF source.

A finalized meeting artifact becomes ONE distilled SourceEnvelope: title,
date, platform, participants, customer/project/topic when known, summary,
decisions, actions (owner + due), risks and open questions. The raw
transcript is NEVER part of it — transcripts are PII and stay in the
artifact store (CONTEXT.md hard constraint 6).

Identity and visibility come straight from the artifact's own policy, mapped
onto DF's fail-closed ACL vocabulary:

  visibility='org'          -> acl_mode='org_default'  (whole org may read)
  visibility='participants' -> acl_mode='mirrored'     (attendee identities)
  visibility='private'      -> acl_mode='mirrored'     (dispatcher only)
  ...and if no attendee can be resolved to a stable identity, the envelope
  degrades to acl_mode='unknown', which stores ZERO ACL rows — visible to
  nobody rather than to everybody. Meeting rosters are speaker DISPLAY NAMES,
  so an attendee only becomes an identity when an email is available.

Ingestion is idempotent and versioned by construction: ``external_id`` is the
meeting id, so re-finalizing the same meeting updates the head and mints a
new immutable version only when content/ACL actually changed (DF's
checksum + meta-change rules). Deleting or expiring a meeting emits a
tombstone envelope, which removes it from retrieval.

Containers give recurring-series grouping for free: ``container_external_id``
is the ledger meeting key, so "the last six months of the Acme call" is a
scope filter, not a scan.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .connectors import SyncBatch
from .envelope import body_checksum

_PLATFORMS = (
    ("meet.google.com", "google_meet"),
    ("zoom.us", "zoom"),
    ("teams.microsoft.com", "microsoft_teams"),
    ("teams.live.com", "microsoft_teams"),
    ("webex.com", "webex"),
)
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_MAX_ITEMS = 40
CONNECTOR_NAME = "Meeting Memory"


def platform_of(meeting_url: str) -> str:
    url = str(meeting_url or "").lower()
    for needle, name in _PLATFORMS:
        if needle in url:
            return name
    return "unknown"


def _clean(value: Any, limit: int = 400) -> str:
    return " ".join(str(value or "").split())[:limit]


def _items(raw: Any) -> list[str]:
    """Artifact list fields are either strings or {text/title/...} dicts."""
    out: list[str] = []
    if isinstance(raw, str):
        raw = [line for line in raw.splitlines() if line.strip()]
    if not isinstance(raw, list):
        return out
    for entry in raw[:_MAX_ITEMS]:
        if isinstance(entry, str):
            text = _clean(entry)
        elif isinstance(entry, dict):
            text = _clean(
                entry.get("text") or entry.get("title")
                or entry.get("decision") or entry.get("risk")
                or entry.get("question") or entry.get("summary")
            )
        else:
            text = ""
        if text:
            out.append(text)
    return out


def _actions(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for entry in raw[:_MAX_ITEMS]:
        if isinstance(entry, str):
            title, owner, due = _clean(entry), "", ""
        elif isinstance(entry, dict):
            title = _clean(
                entry.get("title") or entry.get("text") or entry.get("action")
            )
            owner = _clean(entry.get("owner") or entry.get("assignee"), 120)
            due = _clean(entry.get("due") or entry.get("due_date"), 60)
        else:
            continue
        if title:
            out.append({"title": title, "owner": owner, "due": due})
    return out


def _participants(artifact: dict, meeting_meta: dict) -> list[dict[str, str]]:
    """Distinct participants with an email when one is known. Recall rosters
    carry display names only, so emails arrive from the calendar/integration
    attendee list when the orchestrator supplies one."""
    by_name: dict[str, dict[str, str]] = {}

    def add(name: str, email: str = "") -> None:
        name = _clean(name, 120)
        email = _clean(email, 200).lower()
        if not name and not email:
            return
        # The same human arrives twice — once from the calendar attendee list
        # (email-keyed) and once from the speaker roster (name-only). Merge on
        # either identifier so a participant is listed and ACL'd once.
        key = ""
        for existing_key, entry in by_name.items():
            if email and entry["email"] == email:
                key = existing_key
                break
            if name and entry["name"].lower() == name.lower():
                key = existing_key
                break
        if not key:
            key = (email or name).lower()
        entry = by_name.setdefault(key, {"name": name, "email": email})
        if email and not entry["email"]:
            entry["email"] = email
        if name and not entry["name"]:
            entry["name"] = name

    for att in (meeting_meta.get("attendees") or [])[:_MAX_ITEMS]:
        if isinstance(att, str):
            found = _EMAIL.search(att)
            add(att if not found else att.replace(found.group(0), "").strip(),
                found.group(0) if found else "")
        elif isinstance(att, dict):
            add(str(att.get("name") or att.get("displayName") or ""),
                str(att.get("email") or att.get("mail") or ""))
    for row in (artifact.get("participation") or [])[:_MAX_ITEMS]:
        if isinstance(row, dict):
            add(str(row.get("name") or ""), str(row.get("email") or ""))
        elif isinstance(row, str):
            add(row)
    return list(by_name.values())


def distill(artifact: dict, *, bot_id: str,
            meeting_meta: dict | None = None) -> dict[str, Any]:
    """The indexed field set for one finalized meeting. Pure: no DB, no IO.
    Never includes the transcript."""
    meta = meeting_meta or {}
    saved_at = float(artifact.get("saved_at") or 0.0)
    when = ""
    if saved_at:
        when = datetime.fromtimestamp(saved_at, tz=timezone.utc).isoformat()
    meeting_url = str(artifact.get("meeting_url") or meta.get("url") or "")
    return {
        "meeting_id": str(bot_id),
        "title": _clean(meta.get("title") or artifact.get("title")
                        or artifact.get("meeting_type") or "Meeting", 300),
        "date": when,
        "platform": platform_of(meeting_url),
        "source_reference": meeting_url,
        "participants": _participants(artifact, meta),
        "customer": _clean(meta.get("customer") or meta.get("account"), 200),
        "project": _clean(meta.get("project"), 200),
        "topics": [_clean(t, 80) for t in (meta.get("topics") or [])[:12]
                   if _clean(t, 80)],
        "meeting_type": _clean(artifact.get("meeting_type"), 80),
        "summary": _clean(artifact.get("summary"), 4000),
        "decisions": _items(artifact.get("decisions")),
        "actions": _actions(artifact.get("actions")),
        "risks": _items(artifact.get("risks")),
        "open_questions": _items(
            artifact.get("open_questions") or artifact.get("missing_steps")
        ),
    }


def render_body(fields: dict[str, Any]) -> str:
    """The retrievable text. Markdown so the existing heading-aware chunker
    gives each section its own chunk with a usable citation section label."""
    lines: list[str] = [f"# {fields['title']}"]
    head = [f"Meeting ID: {fields['meeting_id']}"]
    if fields.get("date"):
        head.append(f"Date: {fields['date']}")
    if fields.get("platform"):
        head.append(f"Platform: {fields['platform']}")
    if fields.get("customer"):
        head.append(f"Customer: {fields['customer']}")
    if fields.get("project"):
        head.append(f"Project: {fields['project']}")
    if fields.get("topics"):
        head.append("Topics: " + ", ".join(fields["topics"]))
    names = [p["name"] or p["email"] for p in fields.get("participants") or []]
    if names:
        head.append("Participants: " + ", ".join(n for n in names if n))
    lines.append("\n".join(head))
    if fields.get("summary"):
        lines.append(f"## Summary\n\n{fields['summary']}")
    if fields.get("decisions"):
        lines.append("## Decisions\n\n"
                     + "\n".join(f"- {d}" for d in fields["decisions"]))
    if fields.get("actions"):
        rows = []
        for action in fields["actions"]:
            bits = [action["title"]]
            if action.get("owner"):
                bits.append(f"owner: {action['owner']}")
            if action.get("due"):
                bits.append(f"due: {action['due']}")
            rows.append("- " + " — ".join(bits))
        lines.append("## Actions\n\n" + "\n".join(rows))
    if fields.get("risks"):
        lines.append("## Risks\n\n"
                     + "\n".join(f"- {r}" for r in fields["risks"]))
    if fields.get("open_questions"):
        lines.append("## Open questions\n\n"
                     + "\n".join(f"- {q}" for q in fields["open_questions"]))
    return "\n\n".join(lines) + "\n"


def _acl_for(artifact: dict, fields: dict) -> tuple[str, list[dict]]:
    """Artifact visibility -> DF ACL. Fail-closed: a participants/private
    meeting whose attendees cannot be resolved to stable identities becomes
    'unknown' (nobody) rather than 'org_default' (everybody)."""
    visibility = str(artifact.get("visibility") or "participants").lower()
    if visibility == "org":
        return "org_default", []
    entries: list[dict] = []
    seen: set[str] = set()
    if visibility == "private":
        principal = str(artifact.get("principal_id") or "")
        if principal:
            entries.append({"principal_kind": "user",
                            "principal_external_id": principal,
                            "access": "owner"})
    else:
        for person in fields.get("participants") or []:
            ident = person.get("email")
            if not ident or ident in seen:
                continue
            seen.add(ident)
            entries.append({"principal_kind": "user",
                            "principal_external_id": ident,
                            "access": "reader"})
        principal = str(artifact.get("principal_id") or "")
        if principal and principal not in seen:
            entries.append({"principal_kind": "user",
                            "principal_external_id": principal,
                            "access": "owner"})
    if not entries:
        return "unknown", []
    return "mirrored", entries


def envelope_for_meeting(
    bot_id: str, artifact: dict, *, meeting_meta: dict | None = None,
    deleted: bool = False,
) -> dict[str, Any] | None:
    """The SourceEnvelope for one finalized meeting (or its tombstone)."""
    if not bot_id:
        return None
    fields = distill(artifact or {}, bot_id=bot_id,
                     meeting_meta=meeting_meta)
    container = ""
    try:
        from ..actions import ledger

        container = ledger.meeting_key(fields["source_reference"]) or ""
    except Exception:  # noqa: BLE001 — grouping is a nicety, never a blocker
        container = ""
    if deleted:
        return {
            "external_id": str(bot_id), "kind": "event",
            "title": fields["title"], "acl_mode": "unknown", "acl": [],
            "deleted": True, "checksum": "", "transform": "meeting@1",
            "container_external_id": container,
            "canonical_url": fields["source_reference"],
        }
    body = render_body(fields)
    acl_mode, acl = _acl_for(artifact or {}, fields)
    return {
        "external_id": str(bot_id),
        "kind": "event",
        "title": fields["title"],
        "body_text": body,
        "mime": "text/markdown",
        "canonical_url": fields["source_reference"],
        "author_external_id": str((artifact or {}).get("principal_id") or ""),
        "container_external_id": container,
        "external_updated_at": fields["date"],
        "acl_mode": acl_mode,
        "acl": acl,
        "deleted": False,
        "checksum": body_checksum(body),
        "transform": "meeting@1",
    }


class MeetingConnector:
    """Event-driven, exactly like the upload connector: finalize emits the
    envelope, and a ``full`` run backfills every artifact the org still has."""

    kind = "meeting"
    mirrors_acl = True

    def sync(self, org_id: str, connector: dict, cursor: dict,
             *, full: bool) -> SyncBatch:
        if not full:
            return SyncBatch([], new_cursor=cursor or {"mode": "event"})
        return SyncBatch(
            backfill_envelopes(org_id),
            new_cursor={"mode": "event", "backfilled": True},
        )


def backfill_envelopes(org_id: str) -> list[dict]:
    """Envelopes for every artifact this org still holds."""
    from .. import control_plane, store

    rows = None
    if control_plane.enabled() and control_plane.is_durable_org(org_id):
        rows = control_plane.list_artifacts(org_id)
    if rows is None:
        rows = store.list_artifacts(org_id)
    out: list[dict] = []
    for row in rows or []:
        artifact = row.get("artifact") if isinstance(row, dict) else None
        bot_id = str((row or {}).get("bot_id") or "")
        if not isinstance(artifact, dict) or not bot_id:
            continue
        artifact = dict(artifact)
        artifact.setdefault("saved_at", row.get("saved_at"))
        artifact.setdefault("visibility", row.get("visibility"))
        env = envelope_for_meeting(bot_id, artifact,
                                   meeting_meta=_meta_of(artifact))
        if env is not None:
            out.append(env)
    return out


def emit_finalized(org_id: str, bot_id: str, artifact: dict,
                   *, meeting_meta: dict | None = None,
                   deleted: bool = False) -> bool:
    """Index (or tombstone) one finalized meeting. Called off the live path
    from the finalize tail — SWALLOWS nothing itself, so the caller decides:
    finalize must never fail because memory did.

    Idempotent: the envelope's external_id is the meeting id, so a re-run
    updates the head and mints a version only on a real change."""
    from . import dal, enabled, sync

    if not enabled():
        return False
    env = envelope_for_meeting(bot_id, artifact or {},
                               meeting_meta=meeting_meta, deleted=deleted)
    if env is None:
        return False
    connector = dal.ensure_connector(org_id, "meeting", CONNECTOR_NAME,
                                     actor="finalize")
    # commit_batch does not materialize bodies (only the sync worker does),
    # so do it here — otherwise the meeting is title-searchable only.
    envelopes = sync.materialize_bodies(org_id, connector, [env])
    stats = dal.commit_batch(org_id, connector["id"], envelopes,
                             new_cursor={"mode": "event"})
    # A mirrored-ACL connector only becomes visible once marked authoritative.
    dal.set_connector_acl_mirrored(org_id, connector["id"], True)
    index_facets(org_id, connector["id"], bot_id, artifact or {},
                 meeting_meta=meeting_meta, deleted=deleted)
    sync._reconcile_retrieval(org_id, stats.pop("affected_docs", []))
    return True


def index_facets(org_id: str, connector_id: str, bot_id: str, artifact: dict,
                 *, meeting_meta: dict | None = None,
                 deleted: bool = False) -> None:
    """Keep the filter projection in lockstep with the record. A tombstone
    needs no facet row — the record cascade removes it — and facets never
    grant visibility, so a miss here can only narrow results."""
    if deleted:
        return
    from . import dal, meeting_memory

    record_id = dal.record_id_for(org_id, connector_id, str(bot_id))
    if not record_id:
        return
    fields = distill(artifact, bot_id=bot_id, meeting_meta=meeting_meta)
    series = ""
    try:
        from ..actions import ledger

        series = ledger.meeting_key(fields["source_reference"]) or ""
    except Exception:  # noqa: BLE001
        series = ""
    meeting_memory.replace_facets(org_id, record_id, fields,
                                  series_key=series)


def _meta_of(artifact: dict) -> dict:
    integration = artifact.get("integration")
    if isinstance(integration, dict):
        meeting = integration.get("meeting")
        if isinstance(meeting, dict):
            return meeting
    meeting = artifact.get("meeting")
    return meeting if isinstance(meeting, dict) else {}
