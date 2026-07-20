"""Meeting → browser bridge (Sable B2/B3, trigger side).

Turns an in-meeting "open <site> and show it" into a live browser view on the
avatar's meeting tile: create an operator session (bound to the org's saved
login when one exists), navigate, and mint the read-only live-view URL the
`talk.html` browser_view overlay renders. All operator work is synchronous DB
+ provider I/O, so callers run these in a threadpool off the transcript→speak
hot path; the delivery (speak/control) stays in main.py where the queue lives.

DELIBERATELY INERT unless BOTH the browser operator and the meeting trigger
flag are on. Nothing here ever raises into the caller — a browser hiccup must
never touch a live meeting.

No transcript text crosses to the operator or the provider: the goal is a
bounded server-built string keyed off a coarse site label, never the raw
utterance (Sable non-negotiable — transcripts are PII).
"""
from __future__ import annotations

from typing import Optional

from .config import settings

# Coarse site label → (start URL when a saved login exists, public fallback URL,
# spoken name). The label is the browser-identity label AND the narration word;
# it is derived by the intent detector from a fixed allowlist, never free text.
_SITES = {
    "asana": ("https://app.asana.com/", "https://help.asana.com/", "Asana"),
}

# Active browser view per meeting (keyed by meeting_ref = bot_id) so a later
# dismiss / meeting-end can close exactly the right operator session. Bounded:
# one entry per live meeting, cleared on close.
_ACTIVE: dict[str, tuple[str, str]] = {}  # meeting_ref -> (org_id, session_id)


def trigger_enabled() -> bool:
    """Both switches: the operator must be on AND the meeting trigger opted in.
    Off by default — with either off this whole module no-ops."""
    from . import browser

    return bool(settings.browser_meeting_trigger_enabled) and browser.enabled()


def site_spoken_name(site_label: str) -> str:
    entry = _SITES.get(site_label)
    return entry[2] if entry else site_label


def open_for_meeting(org_id: str, *, avatar_key: str, site_label: str,
                     meeting_ref: str) -> dict:
    """Create + navigate + present a browser view for a meeting. Returns
    {ok, url, spoken, logged_in} on success or {ok: False, reason, spoken}.
    Sync (threadpool). Never raises."""
    entry = _SITES.get(site_label)
    if entry is None:
        return {"ok": False, "reason": "unknown_site", "spoken": ""}
    app_url, public_url, spoken = entry
    try:
        from .browser import dal, operator

        ident = dal.identity_internal(org_id, site_label)
        logged_in = ident is not None
        target = app_url if logged_in else public_url
        # Close any prior view for this meeting first (one tile at a time).
        _close_existing(org_id, meeting_ref)

        session = operator.create_session(
            org_id, principal="meeting", avatar_key=avatar_key,
            meeting_ref=meeting_ref,
            identity_label=site_label if logged_in else "",
            metadata={"purpose": "meeting_browse", "site": site_label},
        )
        sid = session["id"]
        operator.issue_command(org_id, sid, verb="navigate", url=target,
                               principal="meeting")
        minted = operator.present(org_id, sid, principal="meeting")
        if not minted.get("ok"):
            operator.close_session(org_id, sid, principal="meeting")
            return {"ok": False, "reason": "present_failed", "spoken": spoken}
        viewer = operator.exchange_token(org_id, minted["presentation_token"])
        url = ((viewer or {}).get("viewer") or {}).get("url", "")
        if not url:
            operator.close_session(org_id, sid, principal="meeting")
            return {"ok": False, "reason": "no_viewer_url", "spoken": spoken}
        _ACTIVE[meeting_ref] = (org_id, sid)
        return {"ok": True, "url": url, "spoken": spoken,
                "logged_in": logged_in, "session_id": sid}
    except Exception as exc:  # noqa: BLE001 — a browse fault never hits the meeting
        # Class name only; never a payload/URL/credential.
        return {"ok": False, "reason": type(exc).__name__, "spoken": spoken}


def close_for_meeting(meeting_ref: str) -> bool:
    """Close the meeting's active browser view, if any. Sync, never raises."""
    active = _ACTIVE.pop(meeting_ref, None)
    if active is None:
        return False
    org_id, sid = active
    try:
        from .browser import operator

        operator.close_session(org_id, sid, principal="meeting")
    except Exception:  # noqa: BLE001 — best-effort; TTL/reconcile is the backstop
        return False
    return True


def has_active(meeting_ref: str) -> bool:
    return meeting_ref in _ACTIVE


def _close_existing(org_id: str, meeting_ref: str) -> None:
    active = _ACTIVE.pop(meeting_ref, None)
    if active is None:
        return
    _prev_org, sid = active
    try:
        from .browser import operator

        operator.close_session(_prev_org, sid, principal="meeting")
    except Exception:  # noqa: BLE001
        pass
