"""Per-meeting workspace endpoint — GET /dashboard/meetings/{bot_id}.

Read-only projection tying one meeting's before/during/after together
(overview, distilled summary, canonical actions, a derived timeline, files).
Key-free like the rest of the suite: sqlite in tmp_path, no vendors touched.

Critical properties under test:
- payload shape (overview / summary / actions / timeline / files / decisions);
- the derived timeline is time-ordered and merges capture + decision +
  execution events;
- the transcript is EXCLUDED by default and rides ONLY when the org's
  show_transcripts pref is ON;
- the auth gate answers 401, and an unknown/foreign bot_id answers 404 (never
  leaking existence across the tenant boundary).
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app.main as main_module
from app import ledger, outbox, store
from app.api import dashboard
from app.config import settings

SECRET_LINE = "duccio: the acquisition price is nine million"

ORG_A = "orgA"
ORG_B = "orgB"
USER = {"user_id": "u1", "name": "Test User", "org_id": ORG_A}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)  # shares the sqlite file — needs its tables too
    # Default: act as the logged-in ORG_A user (the four-world gate's cookie
    # path). Individual tests override current_user / gate where needed.
    monkeypatch.setattr(dashboard.auth, "current_user", lambda request: dict(USER))
    return TestClient(main_module.app)


def _seed_artifact(
    bot_id: str, org: str, *, avatar: str = "laura",
    principal: str = "u1", transcript: str = "",
) -> float:
    """Seed a finished-meeting artifact with two captured actions. Returns the
    saved_at stamp the store recorded."""
    art = {
        "org_id": org,
        "avatar_id": avatar,
        "principal_id": principal,
        "summary": "Kickoff went well; sandbox confirmed for day one.",
        "risks": ["Security sign-off still pending"],
        "missing_steps": ["security_approval"],
        "goals": ["Ship the pilot by Q3"],
        "decisions": [{"speaker": "Ben", "decision": "proceed with ACME"}],
        "readiness_score": 78,
        "follow_up_email": {"subject": "ACME kickoff follow-up", "body": "…"},
        "meeting_url": "https://meet.google.com/ws-test",
        "duration_seconds": 1860,
        "actions": [
            {
                "action_id": "act_1",
                "owner": "Ben",
                "item": "Email the DPA to legal",
                "typed": {
                    "type": "email.send",
                    "args": {
                        "to": "legal@acme.com",
                        "subject": "DPA",
                        "body": "Please review the attached DPA.",
                    },
                },
                "execution_route": "native",
            },
            {"action_id": "act_2", "owner": "", "item": "Follow up next week"},
        ],
        "transcript": transcript,
    }
    store.save_artifact(bot_id, art, org_id=org)
    rows = store.list_artifacts(None)
    return next(r["saved_at"] for r in rows if r["bot_id"] == bot_id)


def _seed_capture(org: str, bot_id: str, action_id: str, created_at: float) -> None:
    """A durable queued_actions row + an action_capture_events row so the
    workspace's capture-timestamp read has something to join on."""
    outbox._ensure_schema()
    with store._LOCK, store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO queued_actions "
            "(org_id, bot_id, action_id, action, owner, due, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (org, bot_id, action_id, "Email the DPA to legal", "Ben", "",
             created_at, created_at),
        )
        conn.execute(
            "INSERT INTO action_capture_events "
            "(org_id, action_id, source_event_key, source_fingerprint, created_at) "
            "VALUES (?,?,?,?,?)",
            (org, action_id, "evt-1", "fp-1", created_at),
        )


def test_payload_shape(client):
    _seed_artifact("bot_ws_1", ORG_A)
    resp = client.get("/dashboard/meetings/bot_ws_1")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    data = resp.json()
    assert {"overview", "summary", "actions", "timeline", "files",
            "decisions"} <= set(data)

    ov = data["overview"]
    assert ov["bot_id"] == "bot_ws_1"
    assert ov["avatar_id"] == "laura"
    assert ov["platform"] == "Meet"
    assert ov["duration_seconds"] == 1860

    summ = data["summary"]
    assert summ["summary"].startswith("Kickoff")
    assert summ["risks"] and summ["goals"]
    assert summ["missing_steps"] == ["security_approval"]
    assert summ["decisions_count"] == 1

    # Both captured actions surface as canonical Action views.
    ids = {a["action_id"] for a in data["actions"]}
    assert {"act_1", "act_2"} <= ids

    # decisions is the placeholder key for the sibling PR.
    assert data["decisions"] == []

    # Files include the drafted follow-up email.
    assert any(f["kind"] == "follow_up_email" for f in data["files"])


def test_overview_roster_from_archived_meeting(client):
    """Production reality: finalize calls store.remove(bot_id), so an archived
    meeting has NO live session — the roster must come from the ARCHIVED
    transcript speakers (avatar's own lines excluded), not store.get(bot_id)
    (which is None here). Seeds a finalized meeting with a saved artifact and
    NO live session, and asserts the roster is populated from the archive."""
    transcript = (
        "Ben: Let's confirm the sandbox for day one.\n"
        "laura: Noted — I'll draft the follow-up.\n"
        "Priya: Security sign-off is still pending.\n"
        "Ben: Right, we'll chase that.\n"
    )
    _seed_artifact("bot_ws_roster", ORG_A, transcript=transcript)
    assert store.get("bot_ws_roster") is None  # finalized: no live session

    data = client.get("/dashboard/meetings/bot_ws_roster").json()
    roster = data["overview"]["participants"]
    names = {p["name"] for p in roster}
    assert "Ben" in names
    assert "Priya" in names
    assert "laura" not in names  # the avatar's own lines are not attendance
    # Archived attendees are not "here" — the meeting is over.
    assert all(p["here"] is False for p in roster)
    # Deduped case-insensitively: Ben spoke twice, one roster entry.
    assert sum(1 for p in roster if p["name"] == "Ben") == 1


def test_timeline_is_time_ordered_and_merges_events(client):
    saved_at = _seed_artifact("bot_ws_tl", ORG_A)
    # Capture happened well before finalize.
    _seed_capture(ORG_A, "bot_ws_tl", "act_1", created_at=saved_at - 3600)
    # A human approves, then the action executes.
    ledger.record_action_decision(
        "act_1", org_id=ORG_A, decision="approve", laura_user_id="u1",
    )
    ledger.set_action_status(
        "act_1", "done", "https://mail.google.com/receipt/123", org_id=ORG_A,
    )

    data = client.get("/dashboard/meetings/bot_ws_tl").json()
    timeline = data["timeline"]
    assert timeline, "timeline should not be empty"

    # Strictly non-decreasing in ts — the whole point of the derived list.
    tss = [e["ts"] for e in timeline]
    assert tss == sorted(tss)

    events = [e["event"] for e in timeline]
    assert "meeting_finalized" in events
    assert "action_captured" in events
    assert "action_approved" in events
    assert "action_done" in events

    # Capture precedes decision precedes execution in the ordered list.
    def idx(event: str) -> int:
        return next(i for i, e in enumerate(timeline) if e["event"] == event)

    assert idx("action_captured") < idx("action_approved") <= idx("action_done")

    # The approval carries the approver (action_decisions.laura_user_id).
    approved = next(e for e in timeline if e["event"] == "action_approved")
    assert approved["actor"] == "u1"
    assert approved["action_id"] == "act_1"

    # The capture is anchored to the earlier capture-event stamp, not finalize.
    captured = next(e for e in timeline if e["event"] == "action_captured")
    assert captured["ts"] == pytest.approx(saved_at - 3600)


def test_transcript_excluded_by_default(client):
    _seed_artifact("bot_ws_tx", ORG_A, transcript=SECRET_LINE)
    resp = client.get("/dashboard/meetings/bot_ws_tx")
    assert resp.status_code == 200
    assert SECRET_LINE not in resp.text
    assert "transcript" not in resp.json()["summary"]


def test_transcript_present_only_when_pref_on(client):
    _seed_artifact("bot_ws_tx2", ORG_A, transcript=SECRET_LINE)
    store.set_org_pref(ORG_A, "show_transcripts", "1")
    data = client.get("/dashboard/meetings/bot_ws_tx2").json()
    assert data["summary"]["transcript"] == SECRET_LINE
    assert data["prefs"]["show_transcripts"] is True


def test_unknown_bot_id_is_404(client):
    _seed_artifact("bot_ws_known", ORG_A)
    resp = client.get("/dashboard/meetings/does_not_exist")
    assert resp.status_code == 404


def test_foreign_org_bot_id_is_404(client):
    # Meeting belongs to ORG_B; the ORG_A caller must not see it exist.
    _seed_artifact("bot_ws_foreign", ORG_B)
    resp = client.get("/dashboard/meetings/bot_ws_foreign")
    assert resp.status_code == 404


def test_auth_gate_blocks_unauthenticated(client, monkeypatch):
    _seed_artifact("bot_ws_gate", ORG_A)
    monkeypatch.setattr(dashboard.auth, "current_user", lambda request: None)
    monkeypatch.setattr(
        dashboard.auth, "gate",
        lambda request: JSONResponse({"error": "login required"}, status_code=401),
    )
    resp = client.get("/dashboard/meetings/bot_ws_gate")
    assert resp.status_code == 401
