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
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import auth, avatars, ledger, store
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
    execution state the orchestrator reported back (ledger.action_statuses)."""
    if isinstance(action, dict):
        return {
            "action_id": str(action.get("action_id") or ""),
            "item": str(action.get("item") or action.get("step") or "")[:300],
            "owner": str(action.get("owner") or "")[:80],
            "done": bool(action.get("done") or action.get("status") == "done"),
        }
    return {"action_id": "", "item": str(action)[:300], "owner": "", "done": False}


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

    if control_plane.enabled():
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

    if control_plane.enabled():
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


@router.get("/dashboard")
def dashboard_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "dashboard.html")


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
        if machine_org == settings.demo_org_id:
            machine_org = None  # global bearer: today's unscoped service view
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
    artifact_rows = store.list_artifacts()  # newest first
    meetings = [
        m
        for m in (_meeting_row(r) for r in artifact_rows)
        if visible(m["org_id"])
    ]

    # Execution provenance: decorate each action with the state the brain
    # (Cedric) reported via POST /org/actions/{id}/status — one batched query.
    all_ids = [a["action_id"] for m in meetings for a in m["actions"] if a["action_id"]]
    statuses = ledger.action_statuses(all_ids)
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
    org_rows = _org_connection_rows(caller_org) if caller_org else []
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

    return JSONResponse(
        {
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
            "auth_enabled": auth.enabled(),
            "user": (
                {k: user[k] for k in ("user_id", "email", "name", "picture")}
                if user
                else None
            ),
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

    from .cedric import install_state

    try:
        base_url = settings.public_base_url.rstrip("/")
        target = install_state.install_url(
            user["org_id"],
            avatar_id,
            channel.strip(),
            f"{base_url}/dashboard",
            f"{base_url}/dashboard/connections/brain/slack/complete",
        )
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    # Record that THIS org initiated an install (PR D): /slack/complete binds
    # a secret only to an org with a pending/connected row (or a valid signed
    # state), so a machine caller can never attach credentials to an org that
    # never clicked Connect. An already-connected row is left untouched — a
    # re-install must not degrade a working link if the user abandons OAuth.
    rows = _org_connection_rows(user["org_id"])
    current = next(
        (
            r
            for r in rows
            if r["avatar_id"] == avatar_id and r["provider"] == "cedric-brain"
        ),
        None,
    )
    if current is None or current["status"] != "connected":
        if not _set_connection_all(
            user["org_id"], avatar_id, "cedric-brain", "pending",
            {"channel": channel.strip()},
        ):
            return JSONResponse(
                {"error": "connection persistence failed"}, status_code=503
            )
    return RedirectResponse(target, status_code=302)


@router.post("/dashboard/connections/brain/slack/complete")
async def complete_brain_slack_install(request: Request) -> JSONResponse:
    """Cedric's OAuth callback writes the minted secret back server-to-server.

    Binding rule (PR D): a secret may only be attached to an org that
    INITIATED an install. Proof is either the ``state`` Laura's own
    /slack/start minted (echoed back opaquely by the orchestrator — signature
    + TTL verified, org/avatar must match the POST) or, absent a valid state,
    an existing cedric-brain org_connections row (pending from /slack/start
    or POST /connections/brain, connected for a re-install/secret rotation).
    Neither → 403, nothing written. So a caller holding the machine bearer
    can never bind credentials to an arbitrary org.

    On success the response carries ``org_token`` — a freshly minted PER-ORG
    machine bearer (durable control plane when configured, SQLite fallback)
    for the orchestrator to store and use on all its later Laura calls.
    Returned exactly ONCE; Laura keeps only its hash."""
    from . import cedric, control_plane
    from .cedric import install_state, secret_registry

    # This endpoint carries a credential. Never inherit the key-free/demo
    # fail-open behavior used by public session APIs. A PER-ORG bearer is a
    # recognized machine credential too — scoped below to its own org.
    machine_org = await run_in_threadpool(cedric.resolve_machine_org, request)
    if machine_org is None:
        if not settings.laura_api_token.strip():
            return JSONResponse(
                {"error": "machine auth is not configured"}, status_code=503
            )
        return cedric.auth_error(request) or JSONResponse(
            {"error": "unauthorized"}, status_code=401
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    org_id = str((body or {}).get("org_id") or "").strip()
    avatar_id = str((body or {}).get("avatar_id") or "").strip()
    team_id = str((body or {}).get("team_id") or "").strip()
    channel = str((body or {}).get("channel") or "").strip()
    webhook_secret = str((body or {}).get("webhook_secret") or "").strip()
    webhook_token = str((body or {}).get("webhook_token") or "").strip()
    state = str((body or {}).get("state") or "").strip()
    if not all((org_id, avatar_id, team_id, webhook_secret, webhook_token, state)):
        return JSONResponse({"error": "missing required fields"}, status_code=400)
    if machine_org != settings.demo_org_id and org_id != machine_org:
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
    ):
        return JSONResponse(
            {"error": "state does not match this install"}, status_code=403
        )

    synced = await run_in_threadpool(
        secret_registry.upsert_org_credentials,
        org_id, webhook_secret, webhook_token,
    )
    if not synced:
        return JSONResponse({"error": "registry update failed"}, status_code=503)

    # Laura is authoritative for the bearer Cedric uses when calling Laura.
    # Rotate the labelled token on every completed install so re-installation
    # invalidates the previous credential; never fall back to ephemeral SQLite
    # when the durable control plane is enabled.
    org_token = None
    if control_plane.enabled():
        try:
            org_token = await run_in_threadpool(
                control_plane.rotate_org_token,
                org_id,
                "cedric-slack-install",
            )
            await run_in_threadpool(
                store.revoke_org_tokens, org_id, "cedric-slack-install"
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[dashboard] durable org-token rotation failed ({type(exc).__name__})",
                flush=True,
            )
            return JSONResponse({"error": "org token rotation failed"}, status_code=503)
    else:
        org_token = await run_in_threadpool(
            store.rotate_org_token, org_id, "cedric-slack-install"
        )
    if not org_token:
        return JSONResponse({"error": "org token rotation failed"}, status_code=503)

    persisted = await run_in_threadpool(
        _set_connection_all,
        org_id,
        avatar_id,
        "cedric-brain",
        "connected",
        {"team_id": team_id, "channel": channel},
    )
    if not persisted:
        return JSONResponse({"error": "connection persistence failed"}, status_code=503)
    return JSONResponse({"ok": True, "status": "connected", "org_token": org_token})


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
        return JSONResponse({"error": "login required"}, status_code=401)

    from . import cedric  # local import, same reason as connect_brain's

    rows = await run_in_threadpool(_org_connection_rows, user["org_id"])
    brain = [r for r in rows if r["provider"] == "cedric-brain"]
    connected = next((r for r in brain if r["status"] == "connected"), None)
    if connected is None:
        if any(r["status"] == "pending" for r in brain):
            return JSONResponse({"status": "pending", "connectors": []})
        return JSONResponse({"status": "not_connected", "connectors": []})
    team_id = str((connected.get("config") or {}).get("team_id") or "")
    data = await run_in_threadpool(
        cedric.fetch_org_connectors, user["org_id"], team_id
    )
    if data is None:
        return JSONResponse({"status": "unavailable", "connectors": []})
    return JSONResponse({"status": "ok", **data})


@router.post("/dashboard/connections/brain/disconnect")
async def disconnect_brain_remote(request: Request) -> JSONResponse:
    """Disconnect the brain, remote-revoke FIRST (PR D). Body: {avatar_id}.

    Order matters: Cedric's side is detached (DELETE /api/laura/orgs/{org})
    before ANY local state changes. Only a confirmed revoke — 2xx, or 404 =
    already gone, or no orchestrator configured at all — flips the local row
    to 'disconnected' (+ durable mirror) and drops the org's signing secret
    from the registry. A failed/unreachable revoke answers 502 and leaves
    local state UNTOUCHED: the dashboard must never claim a disconnection the
    orchestrator didn't confirm. Cookie-auth + same-origin (a cross-site POST
    must not be able to tear down a customer's brain link)."""
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
    avatar_id = str((body or {}).get("avatar_id") or "").strip()
    if not avatar_id or avatar_id not in avatars.list_ids():
        return JSONResponse({"error": "unknown avatar_id"}, status_code=400)
    rows = await run_in_threadpool(_org_connection_rows, user["org_id"])
    if not any(
        r["avatar_id"] == avatar_id and r["provider"] == "cedric-brain"
        for r in rows
    ):
        return JSONResponse({"error": "brain is not connected"}, status_code=404)

    from . import cedric  # local import, same reason as connect_brain's
    from .cedric import secret_registry

    # Remote revoke first, while Laura still has Cedric's per-workspace
    # bearer. 404 means the remote side is already detached and cleanup may
    # continue.
    status_code = await run_in_threadpool(cedric.revoke_org, user["org_id"])
    revoked_remotely = status_code is not None and (
        200 <= status_code < 300 or status_code == 404
    )
    if status_code is not None and not revoked_remotely:
        return JSONResponse({"error": "remote_revoke_failed"}, status_code=502)

    # Credential deletion and org-token revocation are release-critical. Any
    # failure leaves the UI connected/retryable; it must never report success
    # while an old bearer can still authenticate after a cache refresh.
    credentials_removed = await run_in_threadpool(
        secret_registry.remove_org_credentials, user["org_id"]
    )
    if not credentials_removed:
        return JSONResponse({"error": "credential_cleanup_failed"}, status_code=502)

    from . import control_plane
    if control_plane.enabled():
        try:
            revoked_tokens = await run_in_threadpool(
                control_plane.revoke_org_tokens,
                user["org_id"],
                "cedric-slack-install",
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[dashboard] durable org-token revoke failed ({type(exc).__name__})",
                flush=True,
            )
            revoked_tokens = False
        if not revoked_tokens:
            return JSONResponse({"error": "token_revoke_failed"}, status_code=502)
    await run_in_threadpool(
        store.revoke_org_tokens, user["org_id"], "cedric-slack-install"
    )

    persisted = await run_in_threadpool(
        _set_connection_all, user["org_id"], avatar_id, "cedric-brain",
        "disconnected", {},
    )
    if not persisted:
        return JSONResponse({"error": "connection persistence failed"}, status_code=503)
    return JSONResponse(
        {
            "provider": "cedric-brain",
            "avatar_id": avatar_id,
            "status": "disconnected",
            "remote_revoked": bool(revoked_remotely),
        }
    )


@router.delete("/dashboard/connections/brain/{avatar_id}")
def disconnect_brain(avatar_id: str, request: Request) -> JSONResponse:
    """LEGACY local-only marker: flip the avatar's brain link to disconnected
    without touching the orchestrator or the secret registry. Prefer
    POST /dashboard/connections/brain/disconnect (remote-revoke-first).
    Kept because frontend/dashboard.html still calls this route (Codex's
    file) — retire it once the button moves to the POST."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    ok = store.set_connection(user["org_id"], avatar_id, "cedric-brain", "disconnected", {})
    if not ok:
        return JSONResponse({"error": "unknown avatar_id"}, status_code=400)
    return JSONResponse({"provider": "cedric-brain", "avatar_id": avatar_id, "status": "disconnected"})
