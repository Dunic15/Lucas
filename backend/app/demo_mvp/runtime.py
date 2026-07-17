"""Meeting-bound Northstar demo entry point (the smallest backend-owned glue).

Starts the Northstar browser demo from an ACTIVE meeting and binds, in one
place: organization, authenticated principal, meeting, the selected avatar +
its published overlay version, the Northstar demo definition + version, the
Browser B1 session (northstar provider), the allowed domain, and the (clamped)
coordinator budgets. It does NOT build a generic Skills runtime — it wires the
existing bounded B1 coordinator to the versioned manifest.

Everything returned is safe public session data: no provider ids/urls, no
screenshot bytes, no planner reasoning, no raw page structure.
"""
from __future__ import annotations

from typing import Any, Optional

from .. import avatar_resolver
from ..browser import coordinator as browser_coordinator
from ..browser import operator as browser_operator
from . import manifest


def start(org_id: str, *, principal: str = "", meeting_ref: str = "",
          avatar_key: str = "") -> dict[str, Any]:
    """Create a meeting-bound Northstar browser session. Returns safe session
    data (Laura's uuid + state + demo binding). Raises the same errors the
    browser operator raises (ProviderUnconfigured/OwnershipError)."""
    from . import enabled

    if not enabled():
        return {"ok": False, "reason": "northstar_demo_disabled"}
    m = manifest.load()
    avatar = (avatar_key or m.get("selected_avatar") or "laura").strip()
    resolved = avatar_resolver.resolve(org_id, avatar)
    overlay_version = int(getattr(resolved, "overlay_version", 0) or 0)
    budgets = manifest.clamped_budgets()
    # The session metadata is the bounded, NON-authoritative demo-run label
    # (org/principal/policy are NEVER taken from it).
    view = browser_operator.create_session(
        org_id, principal=principal, avatar_key=avatar,
        meeting_ref=meeting_ref, provider_name="northstar",
        metadata={"demo_definition_id": m["company_id"],
                  "demo_definition_version": m["demo_version"],
                  "demo_run_id": f"{m['company_id']}:{meeting_ref or 'adhoc'}",
                  "current_checkpoint": "understand-northstar"})
    return {
        "ok": True,
        "session": view,
        "binding": {
            "org_id": org_id,
            "avatar_key": avatar,
            "avatar_overlay_version": overlay_version,
            "meeting_ref": meeting_ref,
            "demo_definition": m["company_id"],
            "demo_version": m["demo_version"],
            "starting_url": m["starting_url"],
            "allowed_domains": m.get("allowed_domains") or [],
            "budgets": budgets,
            "goal": m.get("meeting_goal", ""),
        },
    }


def run(org_id: str, session_id: str, *, principal: str = "",
        planner: Any = None, clock: Optional[Any] = None) -> dict[str, Any]:
    """Run the bounded B1 coordinator toward the manifest goal, starting from
    the manifest's starting URL. Reuses the existing coordinator — no second
    coordinator. Returns the transcript (safe per-step summaries)."""
    from . import enabled

    if not enabled():
        return {"outcome": "disabled", "reason": "northstar_demo_disabled"}
    m = manifest.load()
    # Seed the session at the manifest's starting URL (a read-only navigate
    # through the ONE gated command path).
    browser_operator.issue_command(
        org_id, session_id, verb="navigate", principal=principal,
        url=m["starting_url"], command_id="northstar:seed")
    return browser_coordinator.run(
        org_id, session_id, m.get("meeting_goal", ""), principal=principal,
        planner=planner, clock=clock)


def status(org_id: str, session_id: str, *,
           principal: str = "") -> Optional[dict]:
    """Safe public status for the dashboard adapter: session view + the latest
    non-terminal guarded action bound to this session (if any) + a mapped
    presentation event. No provider internals, no page structure."""
    view = browser_operator.get_session(org_id, session_id, principal=principal)
    if view is None:
        return None
    pending = _pending_browser_action(org_id, session_id)
    return {"session": view, "pending_action_id": pending,
            "presentation_event": _presentation_event(view, pending)}


def _pending_browser_action(org_id: str, session_id: str) -> str:
    """The most recent non-terminal browser action bound to this session, via
    the durable queued_actions index (permission_json.browser_session_id).
    Read-only, org-scoped by RLS."""
    from sqlalchemy import text as sql

    from .. import control_plane

    try:
        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org_id)
            row = conn.execute(
                sql("""
                    SELECT action_id FROM queued_actions
                    WHERE org_id=:o AND execution_route='browser'
                      AND permission_json->>'browser_session_id' = :sid
                      AND execution_status IN ('proposed','approved')
                    ORDER BY created_at DESC LIMIT 1
                """), {"o": org_id, "sid": session_id}).first()
        return str(row[0]) if row else ""
    except Exception:  # noqa: BLE001
        return ""


# Map backend session state (+ a pending guarded action) to the dashboard's
# BrowserPresentation event vocabulary. Presentation-only; no logic here.
def _presentation_event(view: dict, pending_action_id: str) -> str:
    state = str(view.get("state") or "")
    if pending_action_id:
        return "waiting_approval"
    return {
        "creating": "starting", "ready": "ready", "presenting": "observing",
        "closing": "closed", "closed": "closed", "failed": "failed",
        "expired": "expired", "revoked": "closed",
    }.get(state, "ready")
