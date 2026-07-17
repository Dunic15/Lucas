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

import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import (
    action_plane, asana_client, auth, avatars, executor, gemini_ears, ledger,
    outbox, store,
)
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
    """Normalize an artifact action (dict or bare string) for the wire.
    action_id rides along so summary() can decorate each action with the
    execution state the orchestrator reported back (ledger.action_statuses).
    ``typed`` is a bare bool (never the spec itself) so the dashboard can render
    the per-row "Approve & run" control for actions the native executor can run
    (calendar.create_event / email.send) without ever shipping the args."""
    if isinstance(action, dict):
        owner = str(action.get("owner") or "")[:80]
        # UNASSIGNED is a VISIBLE triage state, not a silent dead-end: the
        # producer emits owner="UNASSIGNED"/"" when it couldn't route the action
        # (prompt contract). Surface that + the gap reason so the dashboard can
        # flag "needs an owner" instead of showing a blank, ignorable row.
        unassigned = (not owner) or owner.strip().upper() == "UNASSIGNED"
        return {
            "action_id": str(action.get("action_id") or ""),
            "item": str(action.get("item") or action.get("step") or "")[:300],
            "owner": owner,
            "unassigned": unassigned,
            "gap": str(action.get("gap_type") or "")[:24],
            "done": bool(action.get("done") or action.get("status") == "done"),
            "typed": isinstance(action.get("typed"), dict)
            and bool(action["typed"].get("type")),
        }
    return {"action_id": "", "item": str(action)[:300], "owner": "",
            "unassigned": False, "gap": "", "done": False, "typed": False}


def _delivered(
    actions: list, email: dict, readiness: int, decisions_count: int
) -> list[str]:
    """The captured→DELIVERED story for one meeting, from distilled artifact
    fields ONLY (no Cedric dispatch wiring — that's a deferred track). Short
    chips a buyer reads as "what came OUT of this meeting", so the row proves
    Laura produced outcomes, not just that she took notes. Best-effort: an
    empty list simply means the artifact carried nothing worth surfacing."""
    chips: list[str] = []
    if actions:
        chips.append(f"{len(actions)} action{'s' if len(actions) != 1 else ''}")
    if decisions_count:
        chips.append(
            f"{decisions_count} decision{'s' if decisions_count != 1 else ''}"
        )
    if (email.get("subject") or "").strip():
        chips.append("follow-up email drafted")
    if readiness:
        chips.append(f"readiness {readiness}")
    return chips


def _meeting_row(row: dict) -> dict:
    """One artifact → one dashboard meeting row. Distilled fields only: the
    transcript never leaves the store through this projection."""
    art = row.get("artifact") or {}
    email = art.get("follow_up_email") or {}
    actions = [_action_entry(a) for a in (art.get("actions") or [])[:12]]
    readiness = int(art.get("readiness_score") or 0)
    decisions_count = len(art.get("decisions") or [])
    return {
        "bot_id": row.get("bot_id"),
        "saved_at": row.get("saved_at"),
        "avatar_id": art.get("avatar_id", ""),
        "org_id": art.get("org_id", ""),
        "platform": _platform(art.get("meeting_url", "")),
        "meeting_type": art.get("meeting_type", ""),
        "duration_seconds": int(art.get("duration_seconds") or 0),
        "readiness_score": readiness,
        "summary": str(art.get("summary") or "")[:600],
        "actions": actions,
        "missing_steps": [str(s) for s in (art.get("missing_steps") or [])[:8]],
        "decisions_count": decisions_count,
        "follow_up_subject": str(email.get("subject") or "")[:160],
        # Additive: what this meeting DELIVERED (captured→delivered), derived
        # purely from the fields above so it never leaks transcript or invents.
        "delivered": _delivered(actions, email, readiness, decisions_count),
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


def _description(avatar_id: str) -> str:
    """User-facing one-liner for the dashboard avatar card. Read from the
    avatar.yaml `description` field — NEVER the persona/system prompt, which is
    written to be *spoken to the model* ("You are Laura…") and leaks that framing
    into the owner UI. Falls back to empty so the card simply omits it."""
    import yaml

    p = settings.avatars_dir / avatar_id / "avatar.yaml"
    try:
        return str((yaml.safe_load(p.read_text()) or {}).get("description", "") or "")
    except Exception:
        return ""


# Rough variable cost per live avatar-minute — mostly the Recall bot (~$0.01/min
# on the web_4_core tier) plus modest LLM/TTS. A deliberate, conservative
# ESTIMATE for the usage panel; real invoicing is a later track.
EST_COST_PER_MIN = 0.04

# Manual follow-up work a captured, owner-tagged action item saves a human from
# doing by hand (finding the note, drafting the message, chasing the owner). A
# deliberately conservative ESTIMATE for the ROI panel — the buyer-facing "so
# what", derived from the REAL action counts, never invented data.
FOLLOWUP_MINUTES_SAVED_PER_ACTION = 12


def _avatar_email(avatar_id: str) -> str:
    """The avatar's personal address. The watched inbox IS the default avatar's
    address (bare, no tag — an untagged invite falls back to it); every other
    avatar is a +tag alias of it (avatars.from_invite_email routes the tag)."""
    raw = (settings.calendar_invite_emails or "").split(",")[0].strip()
    if "@" not in raw:
        return ""
    local, _, domain = raw.partition("@")
    base = local.split("+")[0]
    if avatar_id == settings.default_avatar_id:
        return f"{base}@{domain}"
    return f"{base}+{avatar_id}@{domain}"


def _is_avatar_inbox(oauth: dict) -> bool:
    """True when a native Google token belongs to the AVATAR's own invite inbox
    (``calendar_invite_emails``) rather than a real user's personal account.

    Native Google is keyed per-user (``org_id == user_id``), so a logged-in
    user's own connect lands their own token. But if the account that was
    connected IS the avatar's shared inbox (e.g. an early setup connect, or the
    account registered for Recall auto-join), it is NOT that user's personal
    calendar — surfacing it as "your week" is exactly the "why do I see Laura's
    email?" bug. Callers treat such a token as unconnected for a logged-in user
    (→ Connect-your-own-Google), while the key-free demo path is left untouched."""
    email = str((oauth or {}).get("email") or "").lower()
    if not email:
        return False
    bases = {
        b.strip().lower()
        for b in (settings.calendar_invite_emails or "").split(",")
        if b.strip()
    }
    return email in bases


def _org_connection_rows(org_id: str) -> list[dict]:
    """One org's connection rows for reads: the SQLite runtime rows overlaid
    with the durable control-plane mirror when configured — the mirror
    survives the ephemeral store, so a redeploy doesn't blank the Configure
    tab or un-flag a customer's connected tools. Sync DB I/O: call from sync
    handlers, or via run_in_threadpool from async ones."""
    rows = {
        (r["avatar_id"], r["provider"]): r for r in store.connections_for_org(org_id)
    }
    from . import control_plane  # lazy: control_plane imports store at load

    if control_plane.is_durable_org(org_id):
        try:
            durable = control_plane.get_connections(org_id) or []
        except Exception:  # noqa: BLE001 — the local rows still serve the read
            durable = []
        for r in durable:
            rows[(r["avatar_id"], r["provider"])] = r
    return list(rows.values())


def _org_connected(rows: list[dict], provider: str) -> bool:
    return any(r["provider"] == provider and r["status"] == "connected" for r in rows)


def _set_connection_all(
    org_id: str, avatar_id: str, provider: str, status: str, config: dict
) -> bool:
    """Durably upsert one connection, then refresh the SQLite runtime cache.

    When Postgres is enabled it is authoritative: a failed mirror returns
    False instead of acknowledging a connection/disconnect that a redeploy
    would undo. Sync DB I/O: threadpool it from async handlers.
    """
    from . import control_plane  # lazy: control_plane imports store at load

    if control_plane.is_durable_org(org_id):
        try:
            durable = control_plane.set_connection(
                org_id, avatar_id, provider, status, config
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "[dashboard] control-plane connection write failed "
                f"({type(exc).__name__})",
                flush=True,
            )
            return False
        if durable is not True:
            return False
    return store.set_connection(org_id, avatar_id, provider, status, config)


# Served behind the Cloudflare edge (lauravatar.com). Dynamic per-user state
# (summary, live Cedric connectors) must NEVER be cached, or a connect/disconnect
# isn't reflected until a manual/hard refresh: the app already re-fetches after the
# mutation (dashboard.html load()), but a cached GET hands back the pre-change
# state. The backend state itself is fresh (the disconnect saga is synchronous and
# the reads are un-cached) — only the HTTP layer was serving it stale.
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/dashboard")
def dashboard_page() -> FileResponse:
    # no-cache (revalidate), not no-store: the ETag still yields cheap 304s, but a
    # deploy's new dashboard.html is picked up immediately instead of a heuristically
    # cached shell lingering until a hard refresh (it shipped with ETag/Last-Modified
    # but NO Cache-Control, which is exactly what enabled heuristic caching).
    return FileResponse(
        FRONTEND_DIR / "dashboard.html",
        headers={"Cache-Control": "no-cache"},
    )


def _json_safe(obj):
    """Recursively coerce values that the durable Postgres control plane returns
    but stdlib json can't encode — NUMERIC -> Decimal (whole numbers back to
    int, else float) and datetime/date -> ISO string. Prevents a single Decimal
    (e.g. a billing/usage field) from 500ing the whole /dashboard/summary."""
    import datetime
    from decimal import Decimal

    if isinstance(obj, Decimal):
        f = float(obj)
        return int(f) if f.is_integer() else f
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


@router.get("/dashboard/summary")
def dashboard_summary(request: Request) -> JSONResponse:
    # One gate for all four worlds (see auth.gate): logged-in cookie user,
    # PER-ORG machine bearer (scoped like the cookie user, PR D), global
    # machine bearer, or key-free demo. An anonymous browser on a
    # login-enabled deployment gets login_required (the sign-in gate).
    from . import cedric  # local import, same reason as auth.gate's

    user = auth.current_user(request)
    machine_org = None
    if user is None:
        # Sync handler: FastAPI already runs this off the event loop, so the
        # sync token resolver is safe to call inline.
        machine_org = cedric.resolve_machine_org(request)
        if machine_org is None:
            if err := auth.gate(request):
                return err
    # The tenant this response is scoped to: the cookie user's org or the
    # per-org bearer's org. None = the unscoped worlds (global bearer /
    # key-free demo), byte-identical to today.
    caller_org = user["org_id"] if user else machine_org

    # Tenancy scoping: a scoped caller sees their own org's rows plus legacy
    # unowned ("") rows — NOT the Demo org's. Self-serve product decision
    # (2026-07-13): demo/service rows are the anonymous showroom, and a real
    # signup's dashboard must contain only their workspace, or every customer
    # sees every other anonymous demo. Anonymous/demo callers are unchanged.
    def visible(row_org: str) -> bool:
        return caller_org is None or row_org in ("", caller_org)

    now = time.time()
    artifact_scope = None
    if store.durable_artifacts_enabled():
        artifact_scope = caller_org
        # A global deployment bearer is not a cross-tenant archive credential.
        # Preserve its Demo view in configured production.
        if artifact_scope is None:
            artifact_scope = settings.demo_org_id
    # Key-free SQLite remains a global read plus the legacy visibility filter.
    artifact_rows = store.list_artifacts(artifact_scope)  # newest first
    meetings = [
        m
        for m in (_meeting_row(r) for r in artifact_rows)
        if visible(m["org_id"])
    ]

    # Execution provenance: decorate each action with the state the brain
    # (Cedric) reported via POST /org/actions/{id}/status — one batched query.
    all_ids = [a["action_id"] for m in meetings for a in m["actions"] if a["action_id"]]
    statuses = ledger.action_statuses(
        all_ids, org_id=caller_org or settings.demo_org_id
    )
    for m in meetings:
        for a in m["actions"]:
            ex = statuses.get(a["action_id"])
            if ex:
                a["execution"] = {"status": ex["status"], "detail": ex["detail"][:160]}

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
    # ── ORG-level connection state for the per-avatar capability toggles ──
    # An integration is CONNECTED once at the org level (Connections view); each
    # avatar then independently toggles whether it may USE it. Computed ONCE here
    # (reused for the `connections` block below, no double I/O) so every avatar
    # card reads the same truth. Keyed by the org_id string — no ::uuid cast, so
    # u_<hash> and uuid orgs alike are safe (org_id split-brain).
    #   google = the org's NATIVE Google OAuth (store.get_org_oauth) — the same
    #            signal the "Google (native)" connection card reads.
    #   slack  = the org's cedric-brain connection (Slack rides Cedric): a
    #            connected cedric-brain row for any avatar in the org.
    org_rows = _org_connection_rows(caller_org) if caller_org else []
    _native_google_org = caller_org or settings.demo_org_id
    google_connected = bool(store.get_org_oauth(_native_google_org))
    slack_connected = _org_connected(org_rows, "cedric-brain")
    # asana = the org's stored PAT (provider="asana") or the ASANA_TOKEN env
    # fallback — the same signal the executor/join-snapshot eligibility reads.
    asana_connected = asana_client.connected(_native_google_org)
    all_caps = store.all_avatar_capabilities()  # {avatar_id: {cap: bool}} — one read
    # Per-org roster: a scoped caller (cookie user or per-org bearer) sees only
    # their org's granted avatars (org_agents); the unscoped worlds see ALL —
    # today's behavior, key-free demo unchanged (docs/infra/MULTI-TENANCY.md).
    roster_ids = (
        avatars.list_for_org(caller_org) if caller_org else avatars.list_ids()
    )
    for aid in roster_ids:
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
                "persona": _description(a.id),
                "wake_words": a.wake_words,
                "voice_id": a.elevenlabs_voice_id,
                "talk_body": a.talk_body,
                "renderer": a.renderer_readiness,
                "silent": a.silent,
                "knowledge_docs": knowledge,
                "knowledge_topics": _knowledge_topics(a),
                "process_templates": _process_templates(a),
                "capabilities": _capabilities(a, knowledge),
                "drive_folder": bool(a.drive_folder_id),
                # Per-avatar brain choice for the dashboard toggle: the stored
                # choice, else derived from the effective (global) mode.
                "brain": store.get_avatar_brain_mode(a.id)
                or (
                    "gemini"
                    if gemini_ears.mode_for_avatar(a.id) in ("reply", "on")
                    else "cerebras"
                ),
                # Per-avatar capability toggles. `connected` = the ORG-level
                # connection (shared by all cards); `on` = this avatar's stored
                # switch, DEFAULTING to `connected` when the owner has never
                # toggled it. The card renders an interactive toggle when
                # connected, else a greyed "Connect in Connections" hint.
                "capabilities_toggle": {
                    "google": {
                        "on": all_caps.get(aid, {}).get("google", google_connected),
                        "connected": google_connected,
                    },
                    "slack": {
                        "on": all_caps.get(aid, {}).get("slack", slack_connected),
                        "connected": slack_connected,
                    },
                    "asana": {
                        # Petra-only default: Asana defaults ON only for the
                        # avatar built for it (avatar.native_tools). Every other
                        # avatar defaults OFF even when the org connected Asana;
                        # the owner can still toggle it on per avatar.
                        "on": all_caps.get(aid, {}).get(
                            "asana",
                            asana_connected and a.uses_native_tool("asana"),
                        ),
                        "connected": asana_connected,
                    },
                },
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

    actions_30d = sum(len(m["actions"]) for m in recent)
    followups_30d = sum(1 for m in recent if m["follow_up_subject"])
    # Actions the orchestrator (Cedric) actually executed — the execution
    # provenance decorated onto each action above. Honest: 0 until dispatch is
    # wired, never fabricated.
    actions_executed_30d = sum(
        1
        for m in recent
        for a in m["actions"]
        if (a.get("execution") or {}).get("status") == "done"
    )
    stats = {
        "meetings_30d": len(recent),
        "hours_30d": round(sum(m["duration_seconds"] for m in recent) / 3600, 1),
        "actions_30d": actions_30d,
        "followups_30d": followups_30d,
        "avg_readiness_30d": round(sum(scored) / len(scored)) if scored else 0,
        "weekly": weekly,
        # ── outcome / ROI framing (ADDITIVE; the keys above are untouched) ──
        # "What Laura DID", not just notes she took. All derived from the real
        # counts above — actions captured, follow-ups automated, and a
        # conservative estimate of the manual follow-up hours those saved.
        "actions_executed_30d": actions_executed_30d,
        "followups_automated_30d": followups_30d,
        "hours_saved_30d": round(
            actions_30d * FOLLOWUP_MINUTES_SAVED_PER_ACTION / 60, 1
        ),
        "roi_minutes_per_action": FOLLOWUP_MINUTES_SAVED_PER_ACTION,
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
    # A SCOPED caller's calendar/gmail/drive/slack flags come from THEIR
    # org_connections rows only (PR D): a fresh org reads NOT connected even
    # though the platform's global Google/Slack account exists — the global
    # env is Laura's own plumbing, not the customer's connection. voice and
    # meetings stay platform capabilities (the product works for every org
    # through them). Anonymous/demo/global callers keep today's global flags.
    # (org_rows was already read once above for the capability toggles.)
    if caller_org is not None:
        connections = {
            "calendar": _org_connected(org_rows, "calendar"),
            "gmail": _org_connected(org_rows, "gmail"),
            "drive": _org_connected(org_rows, "drive"),
            "slack": _org_connected(org_rows, "slack"),
            "voice": bool(settings.elevenlabs_api_key),
            "meetings": bool(settings.recall_api_key),
        }
    else:
        connections = {
            "calendar": bool(settings.google_calendar_client_id),
            "gmail": bool(settings.google_calendar_client_id)
            and bool(settings.gmail_watch_enabled),
            "drive": drive_connected,
            "slack": bool(settings.slack_webhook_url),
            "voice": bool(settings.elevenlabs_api_key),
            "meetings": bool(settings.recall_api_key),
        }

    # NATIVE Google — Laura's OWN Google OAuth (Calendar + Gmail write scopes),
    # the engine behind the native executor. This is the ONLY signal the "Google
    # (native)" capability toggle reads — deliberately NOT the Cedric connector
    # rows above (calendar/gmail there mean a Cedric/Pipedream connector, a
    # different, optional path). Connected == a per-org refresh token is stored
    # by /oauth/google/callback. Keyed on the caller's org, else the demo/owner
    # org (matching where the callback persists it). Pure SQLite read, no
    # ::uuid cast → safe for u_hash and uuid orgs alike. Bool, so it satisfies
    # the "connections values are all bool" contract. (Same signal the
    # per-avatar `google` capability toggle reads — computed once above as
    # `google_connected`.)
    connections["google_native"] = google_connected
    # NATIVE Asana — the org's OAuth grant or PAT (Connections card) or the
    # env fallback. Same bool contract; the same signal the per-avatar `asana`
    # toggle reads. asana_oauth = the one-click connect button is available
    # (the Asana OAuth app env is configured); without it the card falls back
    # to the paste-a-PAT flow.
    connections["asana"] = asana_connected
    connections["asana_oauth"] = asana_client.oauth_available()

    callback_deliveries = outbox.delivery_rows(
        caller_org or settings.demo_org_id
    )

    return JSONResponse(
        _json_safe({
            "avatars": avatar_rows,
            "live": live,
            "meetings": meetings[:60],
            "stats": stats,
            "billing": billing,
            "connections": connections,
            # Per-org avatar connections (the Configure tab): the brain link
            # and per-avatar Gmail/Calendar/Slack/Drive. Config is non-secret
            # wiring only. Durable mirror overlaid when configured (PR D).
            "org_connections": org_rows,
            # PII-safe callback delivery health. Payloads and callback URLs are
            # never exposed; owners see delivered/failed/next-attempt only.
            "callback_deliveries": callback_deliveries,
            # Which engine executes an APPROVED action (NATIVE-INTEGRATIONS-PLAN
            # "Cedric add-on toggle"). Read-only stub for now — derived from the
            # NATIVE_EXECUTOR flag; a real per-org setting slots in behind this
            # same key later. Lets the dashboard show "who runs my actions".
            "settings": {
                "execution_mode": settings.execution_mode,  # native | cedric
                "native_executor": bool(settings.native_executor),
            },
            "auth_enabled": auth.enabled(),
            "user": (
                {k: user[k] for k in ("user_id", "email", "name", "picture")}
                if user
                else None
            ),
        }),
        headers=_NO_STORE,
    )


@router.post("/dashboard/avatar/{avatar_id}/capability")
async def set_avatar_capability_endpoint(
    avatar_id: str, request: Request
) -> JSONResponse:
    """Owner flips one capability ON/OFF for one avatar from its card.

    The integration is connected ONCE at the org level (Connections view); this
    only records whether THIS avatar may use it — e.g. Google connected once,
    Laura's card ON while Cedric's is OFF. Owner-authed like the other dashboard
    mutations: a logged-in owner + same-origin only (the brain-toggle door), so
    the key-free demo can't flip real behaviour. Keyed by the avatar_id string
    (no org, no ::uuid → org_id split-brain safe). Body: {capability, enabled}.
    Takes effect at the next execute/deliver — no redeploy."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    aid = (avatar_id or "").strip()
    if not aid or aid not in set(avatars.list_ids()):
        return JSONResponse({"error": "unknown avatar_id"}, status_code=404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON is a client error
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    capability = str((body or {}).get("capability") or "").strip().lower()
    enabled = bool((body or {}).get("enabled"))
    ok = await run_in_threadpool(
        store.set_avatar_capability, aid, capability, enabled
    )
    if not ok:
        return JSONResponse(
            {"error": "capability must be one of "
             + ", ".join(store.KNOWN_CAPABILITIES)},
            status_code=400,
        )
    return JSONResponse(
        {"ok": True, "avatar_id": aid, "capability": capability, "enabled": enabled},
        headers=_NO_STORE,
    )


@router.post("/dashboard/connections/asana")
async def connect_asana(request: Request) -> JSONResponse:
    """Connect the org's Asana with a Personal Access Token (docs/ASANA.md).

    Body: {token}. The token is verified LIVE against Asana before anything is
    stored — a typo'd token is a clean 400, never a half-connected state. On
    success it is persisted encrypted per-org (org_oauth, provider="asana";
    the row's email/scopes carry the Asana account email + workspace gid for
    the card), and the response names who/what it authenticated as — the
    token itself is never echoed, logged, or shipped to the browser again.
    Owner-authed like the other dashboard mutations (login + same-origin)."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON is a client error
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    token = str((body or {}).get("token") or "").strip()
    if not token:
        return JSONResponse({"error": "token is required"}, status_code=400)

    info = await run_in_threadpool(asana_client.verify_token, token)
    if not info.get("ok"):
        return JSONResponse(
            {"error": f"Asana rejected the token — {info.get('error', 'unknown')}"},
            status_code=400,
        )
    try:
        stored = await run_in_threadpool(
            lambda: store.set_org_oauth(
                user["org_id"], token, provider="asana",
                email=info.get("email", ""), scopes=info.get("workspace_gid", ""),
            )
        )
    except RuntimeError:
        # set_org_oauth fails CLOSED without an encryption key — surface it as
        # a config problem, not a mystery.
        return JSONResponse(
            {"error": "token storage is not configured (set SESSION_SECRET "
                      "or GOOGLE_TOKEN_ENC_KEY)"},
            status_code=500,
        )
    if not stored:
        return JSONResponse({"error": "could not store the token"}, status_code=500)
    # A new token = a possibly different workspace: drop the snapshot cache so
    # the next join reads the new board, not the old org's cached brief.
    asana_client._reset_brief_cache()
    return JSONResponse(
        {"ok": True, "connected": True,
         "email": info.get("email", ""), "workspace": info.get("workspace", "")},
        headers=_NO_STORE,
    )


@router.post("/dashboard/connections/asana/disconnect")
async def disconnect_asana(request: Request) -> JSONResponse:
    """Remove the org's stored Asana credentials — BOTH the OAuth grant (the
    one-click connect) and a pasted PAT, mirroring Google's disconnect. Note:
    if the deployment sets the ASANA_TOKEN env fallback, the platform-level
    connection remains (the response says so honestly)."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    removed_oauth = await run_in_threadpool(
        lambda: store.clear_org_oauth(user["org_id"], provider="asana-oauth")
    )
    removed_pat = await run_in_threadpool(
        lambda: store.clear_org_oauth(user["org_id"], provider="asana")
    )
    asana_client._reset_brief_cache()
    return JSONResponse(
        {"ok": True, "removed": bool(removed_oauth or removed_pat),
         "still_connected_via_env": bool(settings.asana_token.strip())},
        headers=_NO_STORE,
    )


@router.post("/dashboard/outbox/retry")
async def retry_callback_delivery(request: Request) -> JSONResponse:
    """Owner-triggered retry; delivered idempotency keys are never resent."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
        outbox_id = int((body or {}).get("outbox_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "outbox_id is required"}, status_code=400)
    retry_state = await run_in_threadpool(
        outbox.retry_status, user["org_id"], outbox_id
    )
    if retry_state == "missing":
        return JSONResponse({"error": "delivery not found"}, status_code=404)
    if retry_state == "busy":
        # Never steal an unexpired lease from another App Runner instance.
        return JSONResponse(
            {"error": "delivery is already being attempted"},
            status_code=409,
        )
    if retry_state == "queued":
        await run_in_threadpool(
            outbox.process_due, org_id=user["org_id"], outbox_id=outbox_id
        )
    rows = await run_in_threadpool(outbox.delivery_rows, user["org_id"])
    row = next((item for item in rows if item["id"] == outbox_id), None)
    return JSONResponse(
        {
            "ok": True,
            "retry_state": retry_state,
            "delivery": row,
        }
    )


@router.post("/dashboard/connections/brain")
async def connect_brain(request: Request) -> JSONResponse:
    """Connect an avatar to the orchestrator (Cedric, the brain): store the
    org→Slack-workspace wiring and provision it on Cedric's side when his
    /api/laura/orgs is configured. Body: {avatar_id, team_id, channel?}.
    Requires a logged-in user (the org owner) — machine callers have no org."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required to connect the brain"}, status_code=401)
    # Manual workspace IDs are not proof of Slack ownership. Public linking is
    # OAuth-only through /brain/slack/start and its signed state.
    return JSONResponse(
        {"error": "manual workspace linking is retired; use Slack OAuth"},
        status_code=410,
    )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON is a client error
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    avatar_id = str((body or {}).get("avatar_id") or "").strip()
    team_id = str((body or {}).get("team_id") or "").strip()
    channel = str((body or {}).get("channel") or "").strip()
    if not avatar_id or avatar_id not in avatars.list_ids():
        return JSONResponse({"error": "unknown avatar_id"}, status_code=400)
    if not team_id:
        return JSONResponse({"error": "team_id is required (the Slack workspace id)"}, status_code=400)

    from . import cedric  # local import, same reason as auth.gate's
    from .cedric import secret_registry

    provisioned = await run_in_threadpool(
        cedric.provision_org, user["org_id"], team_id, channel, avatar_id
    )
    registry_synced = False
    if provisioned:
        minted_secret = getattr(provisioned, "webhook_secret", "")
        minted_token = getattr(provisioned, "webhook_token", "")
        if minted_secret and minted_token:
            registry_synced = await run_in_threadpool(
                secret_registry.upsert_org_credentials,
                user["org_id"], minted_secret, minted_token,
            )
    # Both directions must be workspace-scoped. Keep the link pending when
    # either credential or the durable connection row is unavailable.
    status = "connected" if provisioned and registry_synced else "pending"
    persisted = await run_in_threadpool(
        _set_connection_all,
        user["org_id"], avatar_id, "cedric-brain", status,
        {"team_id": team_id, "channel": channel},
    )
    if not persisted:
        return JSONResponse({"error": "connection persistence failed"}, status_code=503)
    return JSONResponse(
        {
            "provider": "cedric-brain", "avatar_id": avatar_id, "status": status,
            # pending == saved here, awaiting the orchestrator's org endpoint
            # (contract step B) or a failed call worth retrying.
            "provisioned": bool(provisioned),
            "registry_synced": registry_synced,
        }
    )


@router.get("/dashboard/connections/brain/slack/start")
def connect_brain_slack_start(
    request: Request, avatar_id: str = "cedric", channel: str = ""
) -> Response:
    """Start Cedric's Slack OAuth install without exposing a team ID field."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not avatar_id or avatar_id not in avatars.list_ids():
        return JSONResponse({"error": "unknown avatar_id"}, status_code=400)

    from . import control_plane
    from .cedric import install_state

    try:
        base_url = settings.public_base_url.rstrip("/")
        target, state = install_state.install_url_and_state(
            user["org_id"],
            avatar_id,
            channel.strip(),
            f"{base_url}/dashboard",
            f"{base_url}/dashboard/connections/brain/slack/complete",
        )
        state_data = install_state.unpack(state)
        nonce = str((state_data or {}).get("nonce") or "")
        if not nonce:
            raise RuntimeError("brain install state could not be checkpointed")
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)

    # Persist the one nonce completion may claim. A connected row stays
    # connected while OAuth is in flight, but its pending nonce is replaced so
    # an older browser tab can never rotate credentials after a newer install.
    if control_plane.is_durable_org(user["org_id"]):
        try:
            durable = control_plane.begin_brain_install(
                user["org_id"], avatar_id, nonce, channel.strip()
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "[dashboard] durable brain-install start failed "
                f"({type(exc).__name__})",
                flush=True,
            )
            durable = False
        if durable is not True:
            return JSONResponse(
                {"error": "connection persistence failed"}, status_code=503
            )
    if not store.begin_brain_install(
        user["org_id"], avatar_id, nonce, channel.strip()
    ):
        return JSONResponse(
            {"error": "connection persistence failed"}, status_code=503
        )
    return RedirectResponse(target, status_code=302)


@router.post("/dashboard/connections/brain/slack/complete")
async def complete_brain_slack_install(request: Request) -> JSONResponse:
    """Cedric's OAuth callback writes the minted secret back server-to-server.

    Binding rule: the signed, expiring ``state`` minted by /slack/start is
    mandatory and its org/avatar/channel must match the POST. The pending nonce
    is compare-and-swapped under the control-plane lock, so an older OAuth tab
    cannot rotate credentials after a newer install.

    On success the response carries a PER-ORG ``org_token`` for Cedric to
    store and use on later Laura calls. It is deterministically derived from the
    verified install nonce: a lost-response retry returns the same raw value,
    while Laura persists only SHA-256(raw). A new nonce rotates the credential."""
    from . import cedric, control_plane
    from .cedric import install_state, secret_registry

    # This endpoint carries a credential. Never inherit the key-free/demo
    # fail-open behavior used by public session APIs. A PER-ORG bearer is a
    # recognized machine credential too — scoped below to its own org.
    provisioning_ok = cedric.provisioning_auth_ok(request)
    # The dedicated bootstrap credential is already sufficient and is not an
    # org token. Do not send it through the cross-tenant token resolver (which
    # would be needless database I/O and can fail during provisioning).
    machine_org = (
        None
        if provisioning_ok
        else await run_in_threadpool(cedric.resolve_machine_org, request)
    )
    if not provisioning_ok and machine_org is None:
        if not settings.cedric_orgs_token.strip():
            return JSONResponse(
                {"error": "provisioning auth is not configured"}, status_code=503
            )
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    org_id = str((body or {}).get("org_id") or "").strip()
    avatar_id = str((body or {}).get("avatar_id") or "").strip()
    team_id = str((body or {}).get("team_id") or "").strip()
    channel = str((body or {}).get("channel") or "").strip()
    webhook_secret = str((body or {}).get("webhook_secret") or "").strip()
    # webhook_token is OPTIONAL: it is the per-org bearer for the future
    # token->org path (Option A). The shipped Cedric install callback carries
    # only webhook_secret today, so requiring the token here left every connect
    # stuck at "pending" (400 missing-required-fields). Tenant isolation still
    # holds without it — the events path is bound by the per-org webhook_secret
    # HMAC, and Laura->Cedric calls use the shared deployment bearer that Cedric
    # accepts. When Cedric starts sending webhook_token, it is stored and used.
    webhook_token = str((body or {}).get("webhook_token") or "").strip()
    state = str((body or {}).get("state") or "").strip()
    if not all((org_id, avatar_id, team_id, webhook_secret, state)):
        return JSONResponse({"error": "missing required fields"}, status_code=400)
    if not provisioning_ok and org_id != machine_org:
        return JSONResponse({"error": "not your org"}, status_code=403)
    if avatar_id not in avatars.list_ids():
        return JSONResponse({"error": "unknown avatar"}, status_code=404)

    # Cedric carries Laura's state opaquely through Slack OAuth. Completion
    # requires that signed proof; a pending row alone is not an authenticator.
    data = install_state.unpack(state)
    if (
        data is None
        or data.get("org_id") != org_id
        or data.get("avatar_id") != avatar_id
        or (
            str(data.get("channel") or "")
            and str(data.get("channel") or "") != channel
        )
    ):
        return JSONResponse(
            {"error": "state does not match this install"}, status_code=403
        )
    nonce = str(data.get("nonce") or "")
    try:
        # Derived only after provisioning auth + signed-state binding passed.
        # Same nonce => same raw token; new nonce => rotation. Only SHA-256(raw)
        # is persisted, so a lost HTTP 200 can be retried without secret escrow.
        org_token = install_state.derive_org_token(org_id, nonce)
    except RuntimeError:
        return JSONResponse({"error": "org token derivation failed"}, status_code=503)

    durable_control_plane = control_plane.is_durable_org(org_id)
    saga = (
        control_plane.complete_brain_install
        if durable_control_plane
        else store.complete_brain_install
    )
    try:
        outcome = await run_in_threadpool(
            saga,
            org_id,
            avatar_id,
            nonce,
            org_token,
            team_id,
            channel,
            webhook_secret,
            webhook_token,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            "[dashboard] brain-install completion failed "
            f"({type(exc).__name__})",
            flush=True,
        )
        return JSONResponse({"error": "org token rotation failed"}, status_code=503)

    if outcome == "stale":
        return JSONResponse({"error": "stale install state"}, status_code=409)
    if outcome == "conflict":
        return JSONResponse({"error": "install replay does not match"}, status_code=409)
    if outcome == "registry_failed":
        return JSONResponse({"error": "registry update failed"}, status_code=503)
    if outcome not in ("applied", "replay"):
        return JSONResponse({"error": "org token rotation failed"}, status_code=503)

    # Postgres is authoritative in production; do not write a second SQLite
    # token/connection after releasing the durable org lock.  Such a warm-cache
    # write could interpose after a disconnect and resurrect stale local state.
    return JSONResponse(
        {
            "ok": True,
            "status": "connected",
            "org_token": org_token,
            "idempotent_replay": outcome == "replay",
        }
    )


@router.get("/dashboard/connections/brain/connectors")
async def brain_connectors(request: Request) -> JSONResponse:
    """The product bridge, Laura side: what the connected brain can touch.
    Proxies Cedric's GET /api/laura/connectors for the caller's org — live
    connector catalog (connected / account label / needs-reconnect) plus his
    browser consent links. PII-light passthrough by contract; nothing stored.
    Requires the logged-in owner. Scoped to THE CALLER'S org only (PR D): the
    upstream query carries their workspace team_id from their own connection
    row, an install mid-flow → {"status":"pending"}, and an org with no brain
    connection at all gets {"status":"not_connected"} + an empty catalog —
    never the global demo team's."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse(
            {"error": "login required"}, status_code=401, headers=_NO_STORE
        )

    from . import cedric  # local import, same reason as connect_brain's

    rows = await run_in_threadpool(_org_connection_rows, user["org_id"])
    brain = [r for r in rows if r["provider"] == "cedric-brain"]
    connected = next((r for r in brain if r["status"] == "connected"), None)
    if connected is None:
        if any(r["status"] == "pending" for r in brain):
            return JSONResponse({"status": "pending", "connectors": []}, headers=_NO_STORE)
        return JSONResponse({"status": "not_connected", "connectors": []}, headers=_NO_STORE)
    team_id = str((connected.get("config") or {}).get("team_id") or "")
    # Live passthrough of Cedric's catalog — no server-side cache here, so a
    # just-connected tool shows immediately; no-store keeps the browser/edge from
    # re-serving a pre-connect snapshot.
    data = await run_in_threadpool(
        cedric.fetch_org_connectors, user["org_id"], team_id
    )
    if data is None:
        return JSONResponse({"status": "unavailable", "connectors": []}, headers=_NO_STORE)
    if data.get("not_linked"):
        # Permanent mismatch: the workspace this org's connection row points at
        # is linked to a DIFFERENT Laura org on Cedric's side (his 404). The
        # safe self-service fix is a fresh Add-to-Slack (full /complete flow —
        # last-write-wins re-link + fresh creds into SSM); the frontend renders
        # that CTA. Never a bare POST /api/laura/orgs (webhook-secret drift).
        return JSONResponse({"status": "not_linked", "connectors": []}, headers=_NO_STORE)
    return JSONResponse({"status": "ok", **data}, headers=_NO_STORE)


def _upcoming_platform(url: str) -> str:
    u = (url or "").lower()
    if "meet.google" in u:
        return "Google Meet"
    if "zoom." in u:
        return "Zoom"
    if "teams." in u or "teams/" in u:
        return "Teams"
    return "Other" if u else "—"


def _native_event_start(ev: dict) -> str:
    """RFC3339/ISO start for a Google Calendar event — ``dateTime`` for a timed
    event, ``date`` for an all-day one — or "" when neither is present."""
    start = ev.get("start") or {}
    return str(start.get("dateTime") or start.get("date") or "")


def _native_event_url(ev: dict) -> str:
    """The join URL for a Google Calendar event, in priority order: hangoutLink,
    then a "video" conferenceData entry point, then a URL-shaped location.
    Returns "" when the event carries no meeting link (guards the null-URL
    dispatch crash class downstream)."""
    link = str(ev.get("hangoutLink") or "").strip()
    if link:
        return link
    conf = ev.get("conferenceData") or {}
    for ep in conf.get("entryPoints") or []:
        if str((ep or {}).get("entryPointType") or "") == "video":
            uri = str((ep or {}).get("uri") or "").strip()
            if uri:
                return uri
    loc = str(ev.get("location") or "").strip()
    if loc.startswith("http://") or loc.startswith("https://"):
        return loc
    return ""


def _calendar_event_ref(user_id: str, calendar_id: str, event_id: str) -> str:
    """Opaque, signed browser reference to one event on the caller's calendar."""
    raw = json.dumps(
        {"u": user_id, "c": calendar_id, "e": event_id},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{payload}.{auth._sign(payload, 'calendar-event')}"


def _read_calendar_event_ref(value: str) -> tuple[str, str, str] | None:
    """Return (user_id, calendar_id, event_id) for a valid signed reference."""
    try:
        payload, signature = str(value or "").rsplit(".", 1)
        if not auth._verify(payload, signature, "calendar-event"):
            return None
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        data = json.loads(raw.decode())
        uid = str(data.get("u") or "")
        calendar_id = str(data.get("c") or "")
        event_id = str(data.get("e") or "")
        if not uid or not calendar_id or not event_id:
            return None
        if len(calendar_id) > 1024 or len(event_id) > 1024:
            return None
        return uid, calendar_id, event_id
    except Exception:  # noqa: BLE001 — hostile browser input
        return None


def _iso_plus_minutes(start_iso: str, minutes: int) -> str:
    """``start_iso`` + ``minutes`` as an ISO8601 string, or "" when start can't be
    parsed. Preserves the original offset (a "Z" is normalised to +00:00)."""
    try:
        dt = datetime.fromisoformat(str(start_iso or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    return (dt + timedelta(minutes=minutes)).isoformat()


@router.get("/dashboard/upcoming")
async def dashboard_upcoming(request: Request) -> JSONResponse:
    """Upcoming meetings — the caller's OWN Google Calendar when they've
    connected native Google, else the avatar's Recall Calendar V2 inbox.

    NATIVE path (preferred when ``store.get_org_oauth`` has a token for the
    caller's org): the events come from the user's own primary calendar
    (``google_client.list_calendar_events``), so each user sees THEIR meetings
    and can dispatch the avatar to any of them from the row.

    RECALL fallback (no native token): the avatar's own invite inbox — invite
    its address to any event and the auto-join webhook dispatches a bot; this
    view shows that queue.

    Either way the response shape is identical (calendar + meetings[]) so the
    frontend is source-agnostic. Read-only + PII-light: titles, counts and the
    join URL the owner needs to dispatch — never attendee addresses. No
    server-side cache (no-store)."""
    user = auth.current_user(request)
    machine_org = None
    if user is None:
        from . import cedric  # local import, same reason as auth.gate's

        machine_org = cedric.resolve_machine_org(request)
        if machine_org is None:
            if err := auth.gate(request):
                return err
    # The org whose native Google token we read — mirrors the dashboard summary
    # (caller_org or demo_org_id) so connect + read always agree on the same org.
    # Keyed by the org_id string; no ::uuid cast (org_id split-brain safe).
    native_org = (user["org_id"] if user else machine_org) or settings.demo_org_id

    # Optional week window (calendar navigation): ?start=<ISO>&days=<n>. When
    # given, the fan-out is bounded to [start, start+days) and PAST events in
    # that window are kept (the grid shows the whole selected week, not just
    # "from now"). Absent → today's open-ended "upcoming" read, unchanged.
    _q = request.query_params
    _win_min, _win_max, _keep_past = "", "", False
    _start = str(_q.get("start") or "").strip()
    if _start:
        try:
            _sdt = datetime.fromisoformat(_start.replace("Z", "+00:00"))
            if _sdt.tzinfo is None:
                _sdt = _sdt.replace(tzinfo=timezone.utc)
            try:
                _days = max(1, min(int(_q.get("days") or 7), 31))
            except (TypeError, ValueError):
                _days = 7
            _win_min = _sdt.isoformat()
            _win_max = (_sdt + timedelta(days=_days)).isoformat()
            _keep_past = True
        except (TypeError, ValueError):
            _win_min = _win_max = ""

    def _load_native(
        org_id: str, oauth: dict, *, principal: str = "", on_rotate=None
    ) -> dict:
        from . import google_client

        cal_email = str(oauth.get("email") or "")
        # principal set → a per-USER token (a shared-org member's OWN calendar):
        # pass the resolved oauth so the fetch uses their token + cache slot.
        # principal "" → org/demo path: google_client resolves by org internally
        # (unchanged, including org refresh-token rotation-persist). org_id is
        # still the ORG for the booked-session mapping below either way.
        res = google_client.list_calendar_events(
            org_id, max_results=(150 if _win_min else 25),
            oauth=(oauth if principal else None),
            principal=principal, on_rotate=on_rotate,
            time_min=_win_min, time_max=_win_max,
        )
        if not res.get("ok"):
            # The org HAS a native token but the read failed — surface a soft
            # "connected but unavailable" state. Do NOT silently fall through to
            # the avatar's Recall calendar (that would show a different inbox).
            return {
                "calendar": {
                    "connected": True,
                    "source": "google",
                    "email": cal_email,
                    "error": "calendar_unavailable",
                },
                "meetings": [],
            }
        now = datetime.now(timezone.utc)
        # meeting_urls this org already has a live/scheduled session for → the
        # row shows the avatar-going treatment (badge) instead of a Send button
        # (one bot + one meter per URL, matching /sessions/start's own dedup).
        # Map url→avatar_id so the calendar block can name WHO is being sent
        # ("🎭 Laura"); last write wins if two sessions share a URL (rare).
        booked = {
            s.meeting_url: s.avatar_id
            for s in store.all_sessions()
            if s.meeting_url and s.org_id == org_id
        }
        # The invite inbox base(s) — an event whose attendees include an avatar's
        # +tag alias is one the avatar is set to auto-join, so it earns the same
        # "🎭 <name>" badge as a live/scheduled session. Only the resolved
        # avatar_id is exposed (never the attendee address — PII-light).
        invite_bases = [
            b for b in (settings.calendar_invite_emails or "").split(",") if b.strip()
        ]
        rows: list[dict] = []
        for ev in res.get("events") or []:
            if str(ev.get("status") or "") == "cancelled":
                continue
            start_raw = _native_event_start(ev)
            try:
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start.tzinfo is None:  # all-day 'date' has no offset
                start = start.replace(tzinfo=timezone.utc)
            # An explicit week window shows the WHOLE week (past days included);
            # the default "upcoming" view still drops already-finished events.
            if not _keep_past and start < now - timedelta(minutes=90):
                continue
            url = _native_event_url(ev)
            end = ev.get("end") or {}
            cal_meta = ev.get("_laura_calendar") or {}
            event_ref = ""
            if user is not None and url and cal_meta.get("id") and ev.get("id"):
                event_ref = _calendar_event_ref(
                    str(user.get("user_id") or ""),
                    str(cal_meta.get("id") or ""),
                    str(ev.get("id") or ""),
                )
            invited = avatars.from_invite_email(
                [str((a or {}).get("email") or "") for a in (ev.get("attendees") or [])],
                invite_bases,
            )
            # WHO is being sent: a live/scheduled session for this URL, else an
            # avatar invited by +tag alias. Empty when neither.
            going_avatar = (booked.get(url, "") if url else "") or invited or ""
            rows.append(
                {
                    "id": str(ev.get("id") or ""),
                    "title": str(ev.get("summary") or "Untitled meeting"),
                    "start_time": start_raw,
                    "end_time": str(end.get("dateTime") or end.get("date") or ""),
                    "platform": _upcoming_platform(url),
                    "has_link": bool(url),
                    "meeting_url": url,
                    "attendees": len(ev.get("attendees") or []),
                    # Friendly source-calendar identity for the week UI. The
                    # Google calendar id/email stays private; only its summary
                    # and display color leave this distillation boundary.
                    "calendar_name": str(cal_meta.get("name") or ""),
                    "calendar_color": str(cal_meta.get("color") or ""),
                    # Signed/opaque: lets the owner add an avatar to this real
                    # Google event without exposing the calendar id/email.
                    "event_ref": event_ref,
                    "auto_join": bool(going_avatar),
                    # WHO is being sent (empty when none) — powers the
                    # "🎭 <name>" badge on the calendar block.
                    "auto_join_avatar": going_avatar,
                }
            )
        rows.sort(key=lambda r: r["start_time"])
        return {
            "calendar": {"connected": True, "source": "google", "email": cal_email},
            # A week window can legitimately hold many more than the default
            # view's 20 (a dense program week: standups + sessions + planning
            # across several calendars) — keep them all so the grid is complete.
            "meetings": rows[: (200 if _win_min else 20)],
        }

    def _load_recall() -> dict:
        from . import recall_client

        cals = [
            c
            for c in recall_client.list_calendars()
            if str(c.get("status") or "") in ("connected", "")
        ]
        if not cals:
            return {
                "calendar": {"connected": False, "connect_url": "/oauth/google/connect"},
                "meetings": [],
            }
        cal = cals[0]
        now = datetime.now(timezone.utc)
        rows: list[dict] = []
        for ev in recall_client.list_calendar_events(calendar_id=str(cal.get("id") or "")):
            if ev.get("is_deleted"):
                continue
            start_raw = str(ev.get("start_time") or "")
            try:
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            # keep meetings still in play: anything that started <90 min ago is
            # plausibly in progress; older ones belong to the Past tab
            if start < now - timedelta(minutes=90):
                continue
            raw = ev.get("raw") or {}
            url = str(ev.get("meeting_url") or "")
            rows.append(
                {
                    "id": str(ev.get("id") or ""),
                    "title": str(raw.get("summary") or "Untitled meeting"),
                    "start_time": start_raw,
                    "end_time": str(ev.get("end_time") or ""),
                    "platform": _upcoming_platform(url),
                    "has_link": bool(url),
                    "meeting_url": url,
                    "attendees": len(raw.get("attendees") or []),
                    "auto_join": store.is_scheduled(str(ev.get("id") or "")),
                    # Recall inbox = the avatar's OWN invite queue; the specific
                    # avatar isn't resolved per-URL here → generic badge.
                    "auto_join_avatar": "",
                }
            )
        rows.sort(key=lambda r: r["start_time"])
        return {
            "calendar": {
                "connected": True,
                "source": "recall",
                "email": str(cal.get("oauth_email") or ""),
            },
            "meetings": rows[:20],
        }

    def _load() -> dict:
        # LOGGED-IN user: read THEIR OWN calendar token (user_oauth), scoped to
        # the human — never the org's shared token. A verified corporate domain
        # maps every colleague onto ONE org_id, so an org-keyed read would show
        # one person's calendar to the whole domain (cross-tenant leak). See
        # store.user_oauth + the /oauth/google/callback dual-write.
        if user is not None:
            uid = user["user_id"]
            u_oauth = store.get_user_oauth(uid)
            if u_oauth and not _is_avatar_inbox(u_oauth):
                def _rot(new_rt: str, _uid: str = uid, _o: dict = u_oauth) -> None:
                    store.set_user_oauth(
                        _uid, new_rt,
                        email=str(_o.get("email") or ""),
                        scopes=str(_o.get("scopes") or ""),
                    )
                return _load_native(
                    native_org, u_oauth, principal=f"user:{uid}", on_rotate=_rot
                )
            # BRIDGE for users who connected BEFORE per-user storage (only an
            # org-level row exists): serve it ONLY when its email is the logged-in
            # user's OWN — so the original connector keeps working with no
            # reconnect, while every colleague (email mismatch) is blocked. This
            # is the immediate cross-tenant-leak stopgap; once they reconnect,
            # the per-user row above takes over.
            o_oauth = store.get_org_oauth(native_org)
            _o_email = str((o_oauth or {}).get("email") or "").strip().lower()
            if (
                o_oauth
                and not _is_avatar_inbox(o_oauth)
                and _o_email
                and _o_email == str(user.get("email") or "").strip().lower()
            ):
                return _load_native(native_org, o_oauth)
            # Logged-in but no personal Google of their own → an empty "connect
            # your Google" state, never the avatar's shared Recall inbox.
            return {
                "calendar": {
                    "connected": False,
                    "source": "google",
                    "connect_url": "/oauth/google/connect",
                },
                "meetings": [],
            }
        # Demo / machine caller (no logged-in user): the org's native token, else
        # the avatar's Recall Calendar V2 inbox. Unchanged.
        oauth = store.get_org_oauth(native_org)
        if oauth:
            return _load_native(native_org, oauth)
        return _load_recall()

    try:
        data = await run_in_threadpool(_load)
    except Exception:
        data = {
            "calendar": {"connected": False, "error": "calendar_unavailable"},
            "meetings": [],
        }
    return JSONResponse(_json_safe(data), headers=_NO_STORE)


@router.post("/dashboard/calendar/event")
async def create_calendar_event_endpoint(request: Request) -> JSONResponse:
    """Owner schedules a Google Calendar event straight from the dashboard week
    grid — and, optionally, adds an avatar so it auto-joins.

    Owner-authed like the other dashboard mutations: a logged-in owner +
    same-origin (the same door as the capability/approve endpoints). The event is
    created on the CALLER's OWN org native Google token
    (``store.get_org_oauth`` → ``google_client.create_calendar_event``) — never a
    shared/global account. When ``avatar_id`` is given, that avatar's invite alias
    (the ``+tag`` address, ``_avatar_email``) is added to the attendees so the
    auto-join webhook dispatches it into the meeting. Keyed by the org_id /
    avatar_id STRINGS (no ``::uuid`` cast → org_id split-brain safe).

    SOFT-FAILS by contract (this is off the live path): returns
    ``{ok:false,error}`` for a bad body / no native token
    (``error:"connect_google"``) / a Google hiccup — it never raises. Logs no
    token, no attendee address, no transcript. Body:
    ``{title, start (ISO), end (ISO) OR duration_min, attendees?[], avatar_id?}``.
    """
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON is a client error
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    from . import google_client  # lazy: mirrors the upcoming/native path

    body = body or {}
    title = str(body.get("title") or body.get("summary") or "").strip()
    start = str(body.get("start") or "").strip()
    if not title or not start:
        return JSONResponse({"ok": False, "error": "title and start are required"})
    # end wins when given; otherwise start + duration_min (default 30, clamped).
    end = str(body.get("end") or "").strip()
    if not end:
        try:
            mins = int(body.get("duration_min") or 30)
        except (TypeError, ValueError):
            mins = 30
        mins = max(5, min(mins, 24 * 60))
        end = _iso_plus_minutes(start, mins)
        if not end:
            return JSONResponse({"ok": False, "error": "start must be an ISO datetime"})
    # Attendees: any explicit emails, plus (optionally) the avatar's invite alias
    # so it auto-joins. avatar_id is validated against the listable registry.
    attendees = google_client._emails(body.get("attendees"))
    avatar_id = str(body.get("avatar_id") or "").strip()
    avatar_email = ""
    if avatar_id:
        if avatar_id not in set(avatars.list_ids()):
            return JSONResponse({"ok": False, "error": "unknown avatar_id"})
        avatar_email = _avatar_email(avatar_id)
        if avatar_email and avatar_email.lower() not in {a.lower() for a in attendees}:
            attendees.append(avatar_email)
    # Schedule on the caller's OWN calendar — the per-USER token (user_oauth),
    # NEVER the org's shared token: in a shared org (verified domain → one
    # org_id) an org-keyed write would create events on whichever colleague
    # connected first. Mirrors dashboard_upcoming's _load(): user_oauth wins;
    # else a pre-per-user org row ONLY when its email is this user's own
    # (bridge); else prompt to connect. Avatar-inbox tokens are never scheduled
    # on (never write to the avatar's own calendar).
    native_org = (user["org_id"] if user else "") or settings.demo_org_id
    uid = user["user_id"]
    # Wrapped so a store hiccup degrades to the same soft "connect_google" the
    # docstring promises (never a 500) — mirrors dashboard_upcoming's _load()
    # try/except. on_rotate runs later inside google_client and is best-effort.
    try:
        _oauth = await run_in_threadpool(store.get_user_oauth, uid)
        _principal = f"user:{uid}"
        _on_rotate = None
        if _oauth and not _is_avatar_inbox(_oauth):
            def _on_rotate(new_rt: str, _uid: str = uid, _o: dict = _oauth) -> None:
                store.set_user_oauth(
                    _uid, new_rt,
                    email=str(_o.get("email") or ""),
                    scopes=str(_o.get("scopes") or ""),
                )
        else:
            _org_oauth = await run_in_threadpool(store.get_org_oauth, native_org)
            _oe = str((_org_oauth or {}).get("email") or "").strip().lower()
            if (
                _org_oauth
                and not _is_avatar_inbox(_org_oauth)
                and _oe
                and _oe == str(user.get("email") or "").strip().lower()
            ):
                _oauth = _org_oauth
                _principal = ""  # org path: create_calendar_event resolves by org
            else:
                _oauth = None
    except Exception:  # noqa: BLE001 — soft-fail, never a 500
        _oauth = None
    if not _oauth:
        return JSONResponse({"ok": False, "error": "connect_google"})
    event: dict = {"title": title, "start": start, "end": end}
    if attendees:
        event["attendees"] = attendees
    tz = str(body.get("timezone") or body.get("time_zone") or "").strip()
    if tz:
        event["timezone"] = tz
    res = await run_in_threadpool(
        google_client.create_calendar_event,
        native_org,
        event,
        oauth=(_oauth if _principal else None),
        principal=_principal,
        on_rotate=_on_rotate,
    )
    if not res.get("ok"):
        return JSONResponse(
            {"ok": False, "error": res.get("error") or "calendar_failed"}
        )
    return JSONResponse(
        {
            "ok": True,
            "event_id": res.get("event_id", ""),
            "html_link": res.get("event_url", ""),
            # The provisioned Google Meet join link (empty if none) — the UI can
            # show/confirm it; the avatar joins THIS Meet via its invite alias.
            "meet_url": res.get("meet_url", ""),
            "avatar_added": bool(avatar_email),
        },
        headers=_NO_STORE,
    )


@router.post("/dashboard/calendar/event/avatar")
async def add_avatar_to_calendar_event(request: Request) -> JSONResponse:
    """Invite one Laura avatar into an existing event on the caller's Google.

    This patches the real Google event's attendee list with the avatar invite
    alias. Google sends the invite and the existing Recall calendar watcher
    schedules the avatar for the event.
    """
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
    body = body or {}
    ref = _read_calendar_event_ref(str(body.get("event_ref") or ""))
    if not ref or ref[0] != str(user["user_id"]):
        return JSONResponse({"ok": False, "error": "invalid_event_ref"}, status_code=400)
    _, calendar_id, event_id = ref
    avatar_id = str(body.get("avatar_id") or "").strip()
    if avatar_id not in set(avatars.list_ids()):
        return JSONResponse({"ok": False, "error": "unknown avatar_id"}, status_code=400)
    avatar_email = _avatar_email(avatar_id)
    if not avatar_email:
        return JSONResponse({"ok": False, "error": "avatar invite email unavailable"})

    native_org = str(user.get("org_id") or "") or settings.demo_org_id
    uid = str(user["user_id"])
    try:
        oauth = await run_in_threadpool(store.get_user_oauth, uid)
        principal = f"user:{uid}"
        on_rotate = None
        if oauth and not _is_avatar_inbox(oauth):
            def on_rotate(new_rt: str, _uid: str = uid, _o: dict = oauth) -> None:
                store.set_user_oauth(
                    _uid, new_rt,
                    email=str(_o.get("email") or ""),
                    scopes=str(_o.get("scopes") or ""),
                )
        else:
            org_oauth = await run_in_threadpool(store.get_org_oauth, native_org)
            org_email = str((org_oauth or {}).get("email") or "").strip().lower()
            if (
                org_oauth
                and not _is_avatar_inbox(org_oauth)
                and org_email
                and org_email == str(user.get("email") or "").strip().lower()
            ):
                oauth = org_oauth
                principal = ""
            else:
                oauth = None
    except Exception:  # noqa: BLE001
        oauth = None
    if not oauth:
        return JSONResponse({"ok": False, "error": "connect_google"})

    from . import google_client
    res = await run_in_threadpool(
        google_client.add_calendar_event_attendee,
        native_org,
        calendar_id,
        event_id,
        avatar_email,
        oauth=(oauth if principal else None),
        principal=principal,
        on_rotate=on_rotate,
    )
    if not res.get("ok"):
        return JSONResponse(
            {"ok": False, "error": res.get("error") or "calendar_update_failed"}
        )
    return JSONResponse(
        {
            "ok": True,
            "event_id": res.get("event_id", event_id),
            "avatar_id": avatar_id,
            "idempotent": bool(res.get("idempotent")),
        },
        headers=_NO_STORE,
    )


@router.post("/dashboard/connections/brain/disconnect")
async def disconnect_brain_remote(request: Request) -> JSONResponse:
    """Disconnect Cedric with a retry-safe, org-wide saga.

    The remote link and credentials belong to the org, not one avatar. A
    durable disconnect_phase is written before/after remote revoke so a later
    retry can resume cleanup without a bearer that may already be deleted.
    """
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    avatar_id = str((body or {}).get("avatar_id") or "").strip()
    if not avatar_id or avatar_id not in avatars.list_ids():
        return JSONResponse({"error": "unknown avatar_id"}, status_code=400)

    rows = await run_in_threadpool(_org_connection_rows, user["org_id"])
    brain = [r for r in rows if r["provider"] == "cedric-brain"]
    if not brain or not any(r["avatar_id"] == avatar_id for r in brain):
        return JSONResponse({"error": "brain is not connected"}, status_code=404)
    if all(r["status"] == "disconnected" for r in brain):
        return JSONResponse(
            {
                "provider": "cedric-brain",
                "avatar_id": avatar_id,
                "status": "disconnected",
                "remote_revoked": True,
            }
        )

    from . import cedric, control_plane
    from .cedric import secret_registry

    async def begin_disconnect_all(phase: str) -> bool:
        """Enter/advance the disconnect saga under the same per-avatar lock
        used by OAuth start/completion.  Postgres is authoritative when
        enabled; SQLite mirrors only after the durable transition succeeds."""
        for row in brain:
            if control_plane.is_durable_org(user["org_id"]):
                try:
                    durable = await run_in_threadpool(
                        control_plane.begin_brain_disconnect,
                        user["org_id"],
                        row["avatar_id"],
                        phase,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(
                        "[dashboard] durable brain disconnect lock failed "
                        f"({type(exc).__name__})",
                        flush=True,
                    )
                    return False
                if durable is not True:
                    return False

            local = await run_in_threadpool(
                store.begin_brain_disconnect,
                user["org_id"],
                row["avatar_id"],
                phase,
            )
            if not local:
                # A redeploy may leave the durable row without its ephemeral
                # cache. Rehydrate it, then take the SQLite transaction lock.
                await run_in_threadpool(
                    store.set_connection,
                    user["org_id"],
                    row["avatar_id"],
                    "cedric-brain",
                    row["status"],
                    dict(row.get("config") or {}),
                )
                local = await run_in_threadpool(
                    store.begin_brain_disconnect,
                    user["org_id"],
                    row["avatar_id"],
                    phase,
                )
            if not local:
                return False
        return True

    async def tombstone_all() -> bool:
        """Revoke the labelled bearer and persist a non-resurrectable marker.
        Durable rows are committed before refreshing the SQLite cache."""
        for row in brain:
            if control_plane.is_durable_org(user["org_id"]):
                try:
                    durable = await run_in_threadpool(
                        control_plane.tombstone_brain_install,
                        user["org_id"],
                        row["avatar_id"],
                    )
                except Exception as exc:  # noqa: BLE001
                    print(
                        "[dashboard] durable brain tombstone failed "
                        f"({type(exc).__name__})",
                        flush=True,
                    )
                    return False
                if durable is not True:
                    return False

        for row in brain:
            local = await run_in_threadpool(
                store.tombstone_brain_install,
                user["org_id"],
                row["avatar_id"],
            )
            if not local:
                # Same cold-cache case as begin_disconnect_all.
                await run_in_threadpool(
                    store.set_connection,
                    user["org_id"],
                    row["avatar_id"],
                    "cedric-brain",
                    "disconnecting",
                    {"disconnect_phase": "remote_revoked"},
                )
                local = await run_in_threadpool(
                    store.tombstone_brain_install,
                    user["org_id"],
                    row["avatar_id"],
                )
            if not local:
                return False
        return True

    remote_done = any(
        str((r.get("config") or {}).get("disconnect_phase") or "")
        == "remote_revoked"
        for r in brain
    )
    revoked_remotely = remote_done

    # Every entry and retry re-enters the saga through the same DB lock used by
    # completion. While this status is set, an old OAuth state is always stale.
    initial_phase = "remote_revoked" if remote_done else "revoke_pending"
    if not await begin_disconnect_all(initial_phase):
        return JSONResponse(
            {"error": "connection persistence failed"}, status_code=503
        )

    if not remote_done:
        # Intent is durable before the irreversible call. If Cedric succeeds
        # but the checkpoint write fails, its DELETE is idempotent on retry.
        status_code = await run_in_threadpool(cedric.revoke_org, user["org_id"])
        revoked_remotely = status_code is not None and (
            200 <= status_code < 300 or status_code == 404
        )
        if status_code is not None and not revoked_remotely:
            # Fail closed: begin_disconnect_all already revoked the inbound
            # org token and fenced every row. Keep disconnecting so a retry can
            # resume remote cleanup; never reopen customer access on a 5xx.
            return JSONResponse({"error": "remote_revoke_failed"}, status_code=502)

        if not await begin_disconnect_all("remote_revoked"):
            return JSONResponse(
                {"error": "disconnect checkpoint failed"}, status_code=503
            )

    # From this point retries skip the remote call and converge on cleanup.
    credentials_removed = await run_in_threadpool(
        secret_registry.remove_org_credentials, user["org_id"]
    )
    if not credentials_removed:
        return JSONResponse({"error": "credential_cleanup_failed"}, status_code=502)

    # The tombstone and labelled-token revoke are one transaction per avatar
    # under the install lock. Never clear this marker: it is what prevents an
    # unexpired pre-disconnect OAuth callback from resurrecting the connection.
    if not await tombstone_all():
        return JSONResponse({"error": "token_revoke_failed"}, status_code=502)

    return JSONResponse(
        {
            "provider": "cedric-brain",
            "avatar_id": avatar_id,
            "status": "disconnected",
            "remote_revoked": bool(revoked_remotely),
        }
    )


# The legacy DELETE /dashboard/connections/brain/{avatar_id} route is gone:
# the dashboard button posts to /dashboard/connections/brain/disconnect
# (remote-revoke-first saga above), so the local-only marker had no callers.


# ─────────────── native approve → execute (the loop's last mile) ────────────

def _find_org_action(caller_org: str, action_id: str) -> tuple[dict, str] | None:
    """The stored artifact action with this ``action_id`` that is VISIBLE to
    ``caller_org`` (its own org, or a legacy unowned '' row), as
    ``(action, acting_avatar_id)``, or None.

    The typed spec to execute lives on the SAVED artifact — the trusted source
    for what an approve should run, never the client's request body. The
    artifact's ``avatar_id`` (stamped at finalize) is the ACTING avatar, so the
    approve seam can honour that avatar's per-avatar capability toggle. Scanning
    artifacts is O(meetings) but this is a dashboard action off the live path.
    Doubles as the org-scope check: a caller can only approve an action inside
    an artifact its own org can see."""
    aid = (action_id or "").strip()
    if not aid:
        return None
    scope = None
    if store.durable_artifacts_enabled():
        scope = caller_org or settings.demo_org_id
    for row in store.list_artifacts(scope):
        art = row.get("artifact") or {}
        row_org = art.get("org_id", "")
        if caller_org is not None and row_org not in ("", caller_org):
            continue
        for a in art.get("actions") or []:
            if isinstance(a, dict) and str(a.get("action_id") or "") == aid:
                return a, str(art.get("avatar_id") or "")
    return None


def _executor_action(typed: dict | None) -> dict | None:
    """Bridge a producer typed spec ``{type, args}`` to the executor's action
    shape (``{type, event|message|task}``). None for a missing/non-native spec
    — the signal to approve-without-executing (Cedric/manual keeps the
    action). Delegates to executor.from_typed so this door and the finalize
    auto-push can never disagree about the bridge."""
    return executor.from_typed(typed)


@router.post("/dashboard/actions/{action_id}/approve")
async def approve_action(action_id: str, request: Request) -> JSONResponse:
    """Approve one finalized meeting action — the NATIVE approval surface.

    Dashboard-authed: a logged-in owner only, scoped to their own org. Marks the
    action ``approved`` in the ledger provenance channel and, when the NATIVE
    executor is ON and the action carries a typed spec (calendar.create_event /
    email.send), runs it on the caller's own Google account via
    ``executor.execute_approved`` — which writes the ``done``/``failed`` receipt
    (event link / message id) back to the SAME ledger channel the dashboard
    reads. With the flag OFF (or the action untyped) it is marked approved and
    nothing executes — byte-identical to today's brokered-to-Cedric behaviour.

    This deliberately does NOT touch the machine-gated
    ``POST /org/actions/{id}/resolve`` (Cedric's) — it reuses the ledger but is a
    separate, human-authed door so the two execution engines never collide."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    aid = (action_id or "").strip()
    if not aid:
        return JSONResponse({"error": "action_id is required"}, status_code=400)
    org = user["org_id"]

    found = await run_in_threadpool(_find_org_action, org, aid)
    if found is None:
        return JSONResponse(
            {"error": "unknown action for this org"}, status_code=404,
            headers=_NO_STORE,
        )
    action, acting_avatar = found
    # Edited params (the needs_details loop) win over the artifact's original
    # spec — still never the client's request body (canonical Action plane).
    typed = await run_in_threadpool(
        lambda: ledger.effective_typed(aid, action.get("typed"), org_id=org)
    )

    # A rejected action is CLOSED. The monotonic status guard would keep the
    # chip 'rejected' anyway, but without this check the executor below would
    # still RUN the action — refuse outright; un-rejecting isn't a thing.
    current = await run_in_threadpool(ledger.action_statuses, [aid], org_id=org)
    if (current.get(aid) or {}).get("status") == "rejected":
        return JSONResponse(
            {"error": "action was rejected", "action_id": aid,
             "status": current.get(aid)},
            status_code=409, headers=_NO_STORE,
        )

    # needs_details gate: approving a typed spec with missing REQUIRED fields
    # would execute a broken call or silently no-op ('approved but nothing
    # happened') — surface the exact fields for the edit affordance instead.
    missing = action_plane.missing_params(typed)
    if typed and missing:
        await run_in_threadpool(
            ledger.set_action_status, aid, "needs_details",
            "missing: " + ", ".join(missing), org_id=org,
        )
        return JSONResponse(
            {"error": "needs_details", "action_id": aid,
             "missing_params": missing,
             "params_schema": action_plane.params_schema(typed)},
            status_code=422, headers=_NO_STORE,
        )

    # Record THE canonical decision (first write wins across surfaces and
    # instances). A dashboard approve after a Slack decision — or a repeated
    # dashboard click racing itself — answers from the recorded row instead
    # of executing again.
    recorded_now = await run_in_threadpool(
        lambda: ledger.record_action_decision(
            aid, org_id=org, decision="approve", decided_via="dashboard",
            laura_user_id=str(user.get("user_id") or ""),
            previous_status=(current.get(aid) or {}).get("status") or "proposed",
            new_status="approved",
        )
    )
    if not recorded_now:
        recorded = await run_in_threadpool(
            lambda: ledger.get_action_decision(aid, org_id=org)
        )
        if recorded is not None and recorded["decision"] != "approve":
            return JSONResponse(
                {"error": "decision_conflict", "action_id": aid,
                 "current_status": recorded["new_status"],
                 "decided_via": recorded["decided_via"]},
                status_code=409, headers=_NO_STORE,
            )
        latest = await run_in_threadpool(
            ledger.action_statuses, [aid], org_id=org
        )
        return JSONResponse(
            {
                "ok": True, "action_id": aid, "approved": True,
                "executed": False, "idempotent_replay": True,
                "capability_blocked": False, "typed": bool(typed),
                "execution_mode": settings.execution_mode,
                "status": latest.get(aid),
            },
            headers=_NO_STORE,
        )

    # Mark approved (non-terminal, monotonic) in the shared provenance channel.
    # Best-effort: a durable-org no-op here (a native action has no Cedric
    # queued_actions row) must not fail the approval — execution is the point.
    await run_in_threadpool(
        ledger.set_action_status, aid, "approved", "approved via dashboard",
        org_id=org,
    )

    exec_action = _executor_action(typed)
    executed = False
    capability_blocked = False
    new_status = "approved"
    if exec_action is not None and executor.handles(exec_action):
        # CAPABILITY GATE: the native executor runs an action ONLY when the
        # acting avatar's toggle for that action's FAMILY (google for
        # calendar/gmail, asana for tasks) is on. Read raw and skip on an
        # explicit OFF — an untouched avatar keeps today's behaviour (default
        # on when the org connected that integration, and the clients
        # soft-fail anyway when it hasn't). A blocked action stays
        # `approved`, byte-identical to the executor being off.
        caps = await run_in_threadpool(
            store.get_avatar_capabilities, acting_avatar
        )
        if caps.get(executor.capability_family(exec_action.get("type"))) is False:
            capability_blocked = True
        else:
            # EXECUTION CLAIM (canonical Action plane): the atomic CAS to
            # 'executing' is the only license to call a vendor — a concurrent
            # approve on another surface/instance loses the claim and reports
            # instead of writing twice.
            claimed = await run_in_threadpool(
                lambda: ledger.claim_action_execution(
                    aid, org_id=org,
                    idempotency_key=action_plane.execution_idempotency_key(aid),
                    via="dashboard",
                )
            )
            if claimed:
                # execute_approved writes its own done/failed receipt to the ledger.
                result = await run_in_threadpool(
                    executor.execute_approved, org, aid, exec_action
                )
                executed = True
                new_status = "done" if result.get("ok") else "failed"
    await run_in_threadpool(
        lambda: ledger.set_action_decision_result(
            aid, org_id=org, new_status=new_status,
            execution_job_id=uuid.uuid4().hex if executed else None,
        )
    )

    # Echo the latest provenance (status + distilled detail: event link /
    # message id / error) — the receipt the row will render. Never transcript.
    latest = await run_in_threadpool(ledger.action_statuses, [aid], org_id=org)
    return JSONResponse(
        {
            "ok": True,
            "action_id": aid,
            "approved": True,
            "executed": executed,
            "capability_blocked": capability_blocked,
            "typed": bool(typed),
            "execution_mode": settings.execution_mode,
            "status": latest.get(aid),
        },
        headers=_NO_STORE,
    )


@router.post("/dashboard/actions/{action_id}/reject")
async def reject_action(action_id: str, request: Request) -> JSONResponse:
    """Reject one finalized meeting action — the approve door's mirror.

    Same auth + org scoping as approve. Marks the action ``rejected`` in the
    ledger provenance channel — a TERMINAL status, so ``set_action_status``
    also closes the matching ledger item and the monotonic guard means a late
    replay can't repaint the chip. Nothing ever executes on this path, and
    the approve door refuses a rejected action (409) so it can't be run later.

    If the action already reached a terminal state (done/failed), the mark is
    a monotonic no-op — the response reports the real status rather than
    pretending the reject took."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    aid = (action_id or "").strip()
    if not aid:
        return JSONResponse({"error": "action_id is required"}, status_code=400)
    org = user["org_id"]

    if await run_in_threadpool(_find_org_action, org, aid) is None:
        return JSONResponse(
            {"error": "unknown action for this org"}, status_code=404,
            headers=_NO_STORE,
        )

    # Record the canonical decision (first write wins, best-effort): an
    # undecided action gets its reject on the record; a reject AFTER a prior
    # decision keeps this door's shipped contract — the monotonic status
    # write below is a no-op and the response reports the REAL status (done
    # stays done), never a 409.
    await run_in_threadpool(
        lambda: ledger.record_action_decision(
            aid, org_id=org, decision="reject", decided_via="dashboard",
            laura_user_id=str(user.get("user_id") or ""),
            new_status="rejected",
        )
    )

    await run_in_threadpool(
        ledger.set_action_status, aid, "rejected", "rejected via dashboard",
        org_id=org,
    )
    latest = await run_in_threadpool(ledger.action_statuses, [aid], org_id=org)
    status = latest.get(aid)
    return JSONResponse(
        {
            "ok": True,
            "action_id": aid,
            "rejected": bool(status and status.get("status") == "rejected"),
            "status": status,
        },
        headers=_NO_STORE,
    )


@router.post("/dashboard/actions/{action_id}/params")
async def dashboard_action_params(action_id: str, request: Request) -> JSONResponse:
    """Human door for the needs_details loop: fill/edit an action's typed
    parameters from the dashboard. Same auth as approve; the shared
    org_api.apply_param_edits does the schema validation and locking."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    aid = (action_id or "").strip()
    if not aid:
        return JSONResponse({"error": "action_id is required"}, status_code=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON is a client error
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    args = (body or {}).get("args") if isinstance(body, dict) else None

    from . import org_api  # local import: org_api lazily imports dashboard

    code, payload = await run_in_threadpool(
        org_api.apply_param_edits, user["org_id"], aid, args
    )
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.get("/dashboard/actions/{action_id}")
async def dashboard_action_get(action_id: str, request: Request) -> JSONResponse:
    """The canonical Action object, dashboard-authed (org_api's twin door)."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    aid = (action_id or "").strip()
    if not aid:
        return JSONResponse({"error": "action_id is required"}, status_code=400)

    from . import org_api  # local import: org_api lazily imports dashboard

    view = await run_in_threadpool(
        org_api._canonical_action_view, user["org_id"], aid
    )
    if view is None:
        return JSONResponse(
            {"error": "unknown action for this org"}, status_code=404,
            headers=_NO_STORE,
        )
    return JSONResponse({"action": view}, headers=_NO_STORE)
