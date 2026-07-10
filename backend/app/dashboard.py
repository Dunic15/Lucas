"""Dashboard API — the owner's control view over the avatars.

One aggregation endpoint (`GET /dashboard/summary`) that answers "what are my
avatars, what are they doing right now, and what have they done" from data the
process already produces: the avatar registry, the live session store, and the
saved artifacts. Everything served here is DISTILLED — summaries, action items,
readiness scores, counts — never transcripts (they stay in the artifact store;
this endpoint strips them like cedric.wire_artifact does for webhooks).

Sits behind the same optional Bearer gate as the session API (open when
LAURA_API_TOKEN is unset, so the key-free demo keeps working). Kept as its own
router so main.py stays a 2-line include, like org_api.py.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from . import auth, avatars, store
from .config import settings

router = APIRouter(tags=["dashboard"])

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

_30D = 30 * 24 * 3600
_WEEK = 7 * 24 * 3600


def _platform(meeting_url: str) -> str:
    url = (meeting_url or "").lower()
    if "meet.google" in url:
        return "Meet"
    if "zoom." in url:
        return "Zoom"
    if "teams." in url or "teams.microsoft" in url:
        return "Teams"
    if "webex" in url:
        return "Webex"
    return "—" if not url else "Link"


def _action_entry(action) -> dict:
    """Normalize an artifact action (dict or bare string) for the wire."""
    if isinstance(action, dict):
        return {
            "item": str(action.get("item") or action.get("step") or "")[:300],
            "owner": str(action.get("owner") or "")[:80],
            "done": bool(action.get("done") or action.get("status") == "done"),
        }
    return {"item": str(action)[:300], "owner": "", "done": False}


def _meeting_row(row: dict) -> dict:
    """One artifact → one dashboard meeting row. Distilled fields only: the
    transcript never leaves the store through this projection."""
    art = row.get("artifact") or {}
    email = art.get("follow_up_email") or {}
    return {
        "bot_id": row.get("bot_id"),
        "saved_at": row.get("saved_at"),
        "avatar_id": art.get("avatar_id", ""),
        "org_id": art.get("org_id", ""),
        "platform": _platform(art.get("meeting_url", "")),
        "meeting_type": art.get("meeting_type", ""),
        "duration_seconds": int(art.get("duration_seconds") or 0),
        "readiness_score": int(art.get("readiness_score") or 0),
        "summary": str(art.get("summary") or "")[:600],
        "actions": [_action_entry(a) for a in (art.get("actions") or [])[:12]],
        "missing_steps": [str(s) for s in (art.get("missing_steps") or [])[:8]],
        "decisions_count": len(art.get("decisions") or []),
        "follow_up_subject": str(email.get("subject") or "")[:160],
    }


def _knowledge_docs(avatar: avatars.Avatar) -> int:
    count = 0
    for directory in avatar.knowledge_dirs:
        if directory.exists():
            count += sum(1 for p in directory.glob("*.md"))
    return count


def _prettify(stem: str) -> str:
    """A file stem → a human topic label ('customer_onboarding' → 'Customer
    onboarding')."""
    s = stem.replace("_", " ").replace("-", " ").strip()
    return (s[:1].upper() + s[1:]) if s else stem


def _knowledge_topics(avatar: avatars.Avatar, limit: int = 8) -> list[str]:
    """What the avatar KNOWS — one label per knowledge doc (its heading if the
    file starts with '# Title', else the prettified filename)."""
    topics: list[str] = []
    seen: set[str] = set()
    for directory in avatar.knowledge_dirs:
        if not directory.exists():
            continue
        for p in sorted(directory.glob("*.md")):
            label = _prettify(p.stem)
            try:
                first = p.read_text(errors="ignore").lstrip().splitlines()[0]
                if first.startswith("#"):
                    label = first.lstrip("#").strip()[:60] or label
            except Exception:
                pass
            key = label.lower()
            if key not in seen:
                seen.add(key)
                topics.append(label)
            if len(topics) >= limit:
                return topics
    return topics


def _process_templates(avatar: avatars.Avatar, limit: int = 6) -> list[str]:
    """The meeting checklists this avatar tracks (process_templates/*.yaml)."""
    d = avatar.dir / "process_templates"
    if not d.exists():
        return []
    return [_prettify(p.stem) for p in sorted(d.glob("*.yaml"))][:limit]


def _capabilities(avatar: avatars.Avatar, knowledge: int) -> list[str]:
    """What the avatar can DO — derived from its real config, honest about
    speaking vs silent mode."""
    caps = ["Joins Zoom, Google Meet & Teams live"]
    if avatar.silent:
        caps.append("Listens silently and takes notes (never speaks)")
    else:
        caps.append("Answers out loud, grounded in its docs with citations")
    if knowledge:
        caps.append(f"Grounded on {knowledge} process doc{'s' if knowledge != 1 else ''}")
    caps.append("Tracks the meeting checklist & readiness")
    caps.append("Drafts the post-meeting summary + action items")
    if avatar.drive_folder_id:
        caps.append("Reads a shared Google Drive folder")
    return caps


def _hidden(avatar_id: str) -> bool:
    """Read the avatar.yaml `hidden` flag without depending on the Avatar
    dataclass (a parallel session may own avatars.py). Knowledge-pack folders
    set it so they don't appear as callable avatars in the dashboard."""
    import yaml

    p = settings.avatars_dir / avatar_id / "avatar.yaml"
    try:
        return bool((yaml.safe_load(p.read_text()) or {}).get("hidden", False))
    except Exception:
        return False


# Rough variable cost per live avatar-minute — mostly the Recall bot (~$0.01/min
# on the web_4_core tier) plus modest LLM/TTS. A deliberate, conservative
# ESTIMATE for the usage panel; real invoicing is a later track.
EST_COST_PER_MIN = 0.04


def _avatar_email(avatar_id: str) -> str:
    """The avatar's personal address: the watched inbox with a +tag. Inviting
    it to a meeting/calendar summons THIS avatar (avatars.from_invite_email)."""
    raw = (settings.calendar_invite_emails or "").split(",")[0].strip()
    if "@" not in raw:
        return ""
    local, _, domain = raw.partition("@")
    base = local.split("+")[0]
    return f"{base}+{avatar_id}@{domain}"


@router.get("/dashboard")
def dashboard_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "dashboard.html")


@router.get("/dashboard/summary")
def dashboard_summary(request: Request) -> JSONResponse:
    # One gate for all three worlds (see auth.gate): logged-in cookie user,
    # machine bearer, or key-free demo. An anonymous browser on a login-enabled
    # deployment gets login_required so the page shows the sign-in gate.
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err

    # Tenancy scoping (org_id == user_id today): a logged-in user sees their
    # own rows plus unowned ("") rows — the pre-auth/service world of this
    # single-tenant deployment. Strict isolation lands with the Postgres/RLS
    # track (docs/infra/MULTI-TENANCY.md); the filter seam is already here.
    def visible(row_org: str) -> bool:
        return user is None or row_org in ("", user["org_id"])

    now = time.time()
    artifact_rows = store.list_artifacts()  # newest first
    meetings = [
        m
        for m in (_meeting_row(r) for r in artifact_rows)
        if visible(m["org_id"])
    ]

    # Per-avatar rollups (legacy artifacts predate avatar_id stamping → "").
    by_avatar: dict[str, list[dict]] = {}
    for m in meetings:
        by_avatar.setdefault(m["avatar_id"], []).append(m)

    live = []
    live_by_avatar: dict[str, int] = {}
    for s in store.all_sessions():
        if not visible(s.org_id):
            continue
        live_by_avatar[s.avatar_id] = live_by_avatar.get(s.avatar_id, 0) + 1
        first_ts = s.transcript[0].ts if s.transcript else 0.0
        last_ts = s.transcript[-1].ts if s.transcript else 0.0
        live.append(
            {
                "bot_id": s.bot_id,
                "avatar_id": s.avatar_id,
                "platform": _platform(s.meeting_url),
                "started_at": first_ts,
                "last_activity_at": last_ts,
                "utterances": len(s.transcript),  # count only, never text
                "silent": avatars.load(s.avatar_id).silent
                if s.avatar_id in avatars.list_ids()
                else False,
            }
        )

    avatar_rows = []
    drive_connected = False
    for aid in avatars.list_ids():
        a = avatars.load(aid)
        drive_connected = drive_connected or bool(a.drive_folder_id)
        # Knowledge-pack folders (avatar.yaml `hidden: true`, e.g. sff which
        # Cedric reuses) are not callable avatars — keep them out of the owner
        # dashboard. Read the flag off the yaml so this stays decoupled from the
        # Avatar dataclass. `hidden` avatars still list_ids()/load() normally.
        if _hidden(aid):
            continue
        mine = by_avatar.get(aid, [])
        knowledge = _knowledge_docs(a)
        minutes = round(sum(m["duration_seconds"] for m in mine) / 60)
        avatar_rows.append(
            {
                "id": a.id,
                "name": a.name,
                "role": a.role,
                "email": _avatar_email(a.id),
                "persona": (a.persona_prompt or "")[:220],
                "wake_words": a.wake_words,
                "voice_id": a.elevenlabs_voice_id,
                "talk_body": a.talk_body,
                "silent": a.silent,
                "knowledge_docs": knowledge,
                "knowledge_topics": _knowledge_topics(a),
                "process_templates": _process_templates(a),
                "capabilities": _capabilities(a, knowledge),
                "drive_folder": bool(a.drive_folder_id),
                "live_now": live_by_avatar.get(aid, 0),
                "meetings_total": len(mine),
                "meetings_30d": sum(
                    1 for m in mine if now - (m["saved_at"] or 0) <= _30D
                ),
                "minutes_total": minutes,
                "last_meeting_at": mine[0]["saved_at"] if mine else None,
            }
        )

    recent = [m for m in meetings if now - (m["saved_at"] or 0) <= _30D]
    scored = [m["readiness_score"] for m in recent if m["readiness_score"] > 0]
    weekly = [0] * 8  # meetings per ISO-ish week bucket, oldest → newest
    for m in meetings:
        age = now - (m["saved_at"] or 0)
        bucket = int(age // _WEEK)
        if 0 <= bucket < 8:
            weekly[7 - bucket] += 1

    stats = {
        "meetings_30d": len(recent),
        "hours_30d": round(sum(m["duration_seconds"] for m in recent) / 3600, 1),
        "actions_30d": sum(len(m["actions"]) for m in recent),
        "followups_30d": sum(1 for m in recent if m["follow_up_subject"]),
        "avg_readiness_30d": round(sum(scored) / len(scored)) if scored else 0,
        "weekly": weekly,
    }

    # Usage & billing. Minutes are REAL (summed from meeting durations); the cost
    # is a clearly-labelled estimate and per-avatar breakdown is included. Actual
    # invoicing / plans are a later track (surfaced as "coming soon" in the UI).
    total_minutes = round(sum(m["duration_seconds"] for m in meetings) / 60)
    minutes_30d = round(sum(m["duration_seconds"] for m in recent) / 60)
    per_avatar_min = {}
    for m in meetings:
        per_avatar_min[m["avatar_id"]] = per_avatar_min.get(m["avatar_id"], 0) + (
            m["duration_seconds"] / 60
        )
    billing = {
        "total_minutes": total_minutes,
        "minutes_30d": minutes_30d,
        "est_cost_30d": round(minutes_30d * EST_COST_PER_MIN, 2),
        "rate_per_min": EST_COST_PER_MIN,
        "by_avatar": {k: round(v) for k, v in per_avatar_min.items()},
        "plan": "Demo",  # placeholder — no billing system yet
        "billing_live": False,
    }

    # Booleans only — which integrations are configured, never the secrets.
    connections = {
        "calendar": bool(settings.google_calendar_client_id),
        "gmail": bool(settings.google_calendar_client_id)
        and bool(settings.gmail_watch_enabled),
        "drive": drive_connected,
        "slack": bool(settings.slack_webhook_url),
        "voice": bool(settings.elevenlabs_api_key),
        "meetings": bool(settings.recall_api_key),
    }

    return JSONResponse(
        {
            "avatars": avatar_rows,
            "live": live,
            "meetings": meetings[:60],
            "stats": stats,
            "billing": billing,
            "connections": connections,
            "auth_enabled": auth.enabled(),
            "user": (
                {k: user[k] for k in ("user_id", "email", "name", "picture")}
                if user
                else None
            ),
        }
    )
