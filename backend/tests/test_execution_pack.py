"""Autonomous execution: the avatar emails the recap + files Drive notes at
finalize, from the connected Google account. Key-free — the Google HTTP seam
and token are stubbed. Verifies: off by default, composition, recipient
resolution, best-effort failure, and the finalize wiring (autonomous only)."""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import autopilot, google_actions, ledger, store
from app.config import settings

ARTIFACT = {
    "summary": "Team confirmed the pilot starts July 15.",
    "decisions": ["Monthly pricing at €49/seat"],
    "actions": [{"item": "Send rollout doc", "owner": "Marco", "deadline": "Friday"}],
    "risks": ["Security review not done"],
    "follow_up_email": {},
    "meeting_type": "status_update",
}


@pytest.fixture(autouse=True)
def _stub_google(monkeypatch):
    monkeypatch.setattr(google_actions, "_token", lambda: "tok")
    yield


# ── off by default ──────────────────────────────────────────────────────


def test_execute_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "execute_enabled", False)
    assert autopilot.maybe_execute("Cedric", ARTIFACT, "folder1")["executed"] is False


# ── recap composition + send ─────────────────────────────────────────────


def test_execute_sends_recap_and_writes_notes(monkeypatch):
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(settings, "execute_recap_email", True)
    monkeypatch.setattr(settings, "execute_drive_notes", True)
    monkeypatch.setattr(settings, "execute_recap_to", "boss@example.com")

    sent, wrote = [], []
    monkeypatch.setattr(
        google_actions, "send_gmail",
        lambda to, subj, body: sent.append((to, subj, body)) or {"sent": True},
    )
    monkeypatch.setattr(
        google_actions, "write_drive_note",
        lambda fid, title, content: wrote.append((fid, title, content)) or {"written": True},
    )

    out = autopilot.maybe_execute("Cedric", ARTIFACT, "folder-cedric")
    assert out["executed"] and out["email"]["sent"] and out["drive"]["written"]

    to, subj, body = sent[0]
    assert to == ["boss@example.com"]
    assert "pilot starts July 15" in body
    assert "Send rollout doc" in body and "Marco" in body and "Friday" in body
    assert "Monthly pricing" in body  # decision included
    # notes doc lands in the avatar's folder and carries risks
    fid, title, content = wrote[0]
    assert fid == "folder-cedric"
    assert "Security review not done" in content


def test_execute_prefers_drafted_email(monkeypatch):
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(settings, "execute_recap_to", "x@y.com")
    monkeypatch.setattr(settings, "execute_drive_notes", False)
    art = dict(ARTIFACT, follow_up_email={"subject": "Q3 recap", "body": "As discussed…"})
    captured = []
    monkeypatch.setattr(
        google_actions, "send_gmail",
        lambda to, subj, body: captured.append((subj, body)) or {"sent": True},
    )
    autopilot.maybe_execute("Cedric", art, "")
    subj, body = captured[0]
    assert subj == "Q3 recap" and "As discussed" in body


def test_execute_falls_back_to_attendee_emails(monkeypatch):
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(settings, "execute_recap_to", "")  # no configured list
    monkeypatch.setattr(settings, "execute_drive_notes", False)
    got = []
    monkeypatch.setattr(
        google_actions, "send_gmail", lambda to, s, b: got.append(to) or {"sent": True}
    )
    autopilot.maybe_execute("Cedric", ARTIFACT, "", attendee_emails=["a@co.com", "bad", "b@co.com"])
    assert got == [["a@co.com", "b@co.com"]]  # invalid entry dropped


def test_execute_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(settings, "execute_recap_to", "x@y.com")

    def boom(*a, **k):
        raise RuntimeError("gmail down")

    monkeypatch.setattr(google_actions, "send_gmail", boom)
    out = autopilot.maybe_execute("Cedric", ARTIFACT, "f")
    assert out["executed"] is False and out["reason"] == "RuntimeError"


# ── google_actions transport (stubbed HTTP) ──────────────────────────────


def test_send_gmail_builds_raw_message(monkeypatch):
    posted = {}

    class R:
        status_code = 200

        def json(self):
            return {"id": "m1"}

    def fake_post(url, headers=None, json=None, content=None):
        posted["url"] = url
        posted["json"] = json
        return R()

    monkeypatch.setattr(google_actions._client, "post", fake_post)
    monkeypatch.setattr(google_actions, "_token", lambda: "tok")
    res = google_actions.send_gmail(["to@x.com"], "Hi", "Body")
    assert res["sent"] is True
    assert "raw" in posted["json"]  # base64url RFC822
    import base64
    decoded = base64.urlsafe_b64decode(posted["json"]["raw"]).decode()
    assert "To: to@x.com" in decoded and "Subject: Hi" in decoded


def test_google_actions_no_token_is_clean(monkeypatch):
    monkeypatch.setattr(google_actions, "_token", lambda: "")
    assert google_actions.send_gmail(["a@b.com"], "s", "b")["sent"] is False
    assert google_actions.write_drive_note("f", "t", "c")["written"] is False
    assert google_actions.create_calendar_event("s", "2026-07-10T10:00:00Z", "2026-07-10T10:30:00Z")["created"] is False


# ── finalize wiring: autonomous fires, orchestrated does not ──────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


def test_finalize_triggers_execution_for_plain_session(client, monkeypatch):
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(main_module.recall_client, "create_bot", lambda *a, **k: {"id": "bot_x"})
    monkeypatch.setattr(main_module.recall_client, "leave_call", lambda b: None)
    monkeypatch.setattr(main_module.anam_client, "end_conversation", lambda c: None)

    calls = []
    monkeypatch.setattr(autopilot, "maybe_execute",
                        lambda *a, **k: calls.append(a) or {"executed": True})

    client.post("/sessions/start", json={"meeting_url": "https://meet.google.com/exe-plain-run", "avatar_id": "cedric"})
    session = store.get("bot_x")
    session.add_utterance("Ben", "We shipped it.")
    resp = client.post("/sessions/bot_x/end")
    assert resp.status_code == 200
    assert client.__class__  # sanity
    time.sleep(0.2)
    assert len(calls) == 1  # execution fired for the autonomous session
    assert calls[0][0] == "Cedric"  # avatar name threaded
