"""The action bridge: queue_action live tool -> artifact/ledger + action.requested.

Platform feature (every avatar gets it): during a meeting, "can you send the
recap?" is CAPTURED in-memory on the live session by tools.queue_action —
instant, zero I/O on the live path — then folded into the artifact's actions[]
(and the ledger) at finalize. Orchestrated sessions additionally fire a
best-effort action.requested webhook per capture, so the approval card is
ready before the meeting ends.

Key-free like the rest of the suite: recall/anam are monkeypatched, callbacks
hit local recorders, the brain stays stub.
"""
from __future__ import annotations

import importlib
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import ledger, store, tools
from app.cedric import callback as cedric_callback


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    # Reload store (fresh sqlite at the tmp path) and ledger (recreates its
    # tables in that fresh DB). Reload mutates the module objects in place, so
    # main's `from . import store, ledger` references follow automatically.
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


@pytest.fixture
def recall_stubbed(monkeypatch):
    created: list[dict] = []

    def fake_create_bot(meeting_url, avatar_page_url, join_at=None, bot_name="Laura"):
        created.append({"meeting_url": meeting_url, "bot_name": bot_name})
        return {"id": f"bot_{len(created)}"}

    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(main_module.recall_client, "create_bot", fake_create_bot)
    monkeypatch.setattr(main_module.recall_client, "leave_call", lambda bot_id: None)
    monkeypatch.setattr(main_module.recall_client, "delete_bot", lambda bot_id: None)
    monkeypatch.setattr(
        main_module.anam_client, "end_conversation", lambda conv_id: None
    )
    return created


START_BODY = {
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "avatar_id": "cedric",
    "callback_url": "https://cedric.example/api/meet/callback",
    "external_ref": {"team": "T1", "meet_session_id": "ms_1"},
    "context": {
        "meeting": {"title": "Q3 sync"},
        "brief_markdown": "## Why this meeting\nDiscuss Q3.",
    },
}


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def _wait_until(cond, timeout: float = 2.0) -> bool:
    """Poll for an async/off-thread side effect (the webhook is fire-and-forget
    by design, so tests wait for the recorder instead of the return value)."""
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.01)
    return cond()


# ── (a) dispatch: confirmation + capture on the session ────────────────


def test_queue_action_dispatch_captures_on_session(client):
    session = store.create("bot_a", "https://meet.example/a", "cedric")

    out = tools.dispatch(
        "queue_action",
        {"action": "Send the recap to Marco", "owner": "Ben", "due": "Friday"},
        session=session,
    )
    # Spoken confirmation promises follow-up, never execution.
    assert "queue" in out.lower() and "approval" in out.lower()
    assert not out.lower().startswith("error")
    assert session.queued_actions == [
        {"action": "Send the recap to Marco", "owner": "Ben", "due": "Friday"}
    ]

    # dispatch_for binds the session with the (name, args) signature the LLM
    # tool loop expects.
    bound = tools.dispatch_for(session)
    bound("queue_action", {"action": "Book a follow-up"})
    assert len(session.queued_actions) == 2
    assert session.queued_actions[1] == {
        "action": "Book a follow-up",
        "owner": "",
        "due": "",
    }

    # Validation: an empty action is rejected and captures nothing.
    err = tools.dispatch("queue_action", {"action": "   "}, session=session)
    assert err.startswith("error")
    assert len(session.queued_actions) == 2

    # The session seam is additive: plain tools are untouched, with or
    # without a session threaded in.
    assert tools.dispatch("calculator", {"expression": "2+2"}) == "4"
    assert bound("calculator", {"expression": "3*3"}) == "9"


def test_queue_action_without_session_is_honest():
    out = tools.dispatch("queue_action", {"action": "Send the recap"})
    assert "nothing was queued" in out.lower()


def test_queue_action_registered_in_tool_specs():
    names = [t["function"]["name"] for t in tools.TOOL_SPECS]
    assert "queue_action" in names  # platform-level: every avatar gets it
    spec = next(t for t in tools.TOOL_SPECS if t["function"]["name"] == "queue_action")
    assert spec["function"]["parameters"]["required"] == ["action"]


# ── (b) finalize: captured items merge + dedupe into actions[] ─────────


def test_finalize_merges_and_dedupes_actions(client, recall_stubbed, monkeypatch):
    monkeypatch.setattr(cedric_callback, "send_action_requested", lambda *a: True)
    monkeypatch.setattr(cedric_callback, "send_ended", lambda *a: True)

    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    session = store.get(bot_id)
    # The stub summarizer extracts this line as an action item too ("need").
    session.add_utterance("Ben", "We need to send the recap to Marco")

    tools.dispatch(
        "queue_action",
        # Different case + punctuation than the transcript line: dedupe is on
        # NORMALIZED item text.
        {"action": "we need to send the recap to marco.", "owner": "Ben", "due": "Friday"},
        session=session,
    )
    tools.dispatch(
        "queue_action",
        {"action": "Book a follow-up with the design team"},
        session=session,
    )

    resp = client.post(f"/sessions/{bot_id}/end")
    assert resp.status_code == 200
    actions = resp.json()["actions"]

    # The summarizer's duplicate collapsed into the live capture: exactly one.
    recaps = [a for a in actions if _norm(a["item"]) == "we need to send the recap to marco"]
    assert len(recaps) == 1
    assert recaps[0]["requested_live"] is True  # the live capture won
    assert recaps[0]["owner"] == "Ben"
    assert recaps[0]["deadline"] == "Friday"

    followups = [a for a in actions if _norm(a["item"]) == "book a follow up with the design team"]
    assert len(followups) == 1
    assert followups[0]["owner"] == "UNASSIGNED"
    assert followups[0]["gap_type"] == "owner"  # nobody was named


# ── (c) orchestrated: action.requested fires once per capture ──────────


def test_orchestrated_capture_fires_action_requested(client, recall_stubbed, monkeypatch):
    delivered: list[tuple] = []
    monkeypatch.setattr(
        cedric_callback,
        "send_action_requested",
        lambda integration, bot_id, item: delivered.append((integration, bot_id, item))
        or True,
    )

    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    session = store.get(bot_id)
    tools.dispatch(
        "queue_action",
        {"action": "Send the recap to Marco", "owner": "Ben"},
        session=session,
    )
    tools.dispatch(
        "queue_action", {"action": "Book a follow-up", "due": "Friday"}, session=session
    )

    assert _wait_until(lambda: len(delivered) >= 2)
    time.sleep(0.05)
    assert len(delivered) == 2  # exactly once per capture

    integration, delivered_bot, item = delivered[0]
    assert delivered_bot == bot_id
    assert integration["external_ref"] == {"team": "T1", "meet_session_id": "ms_1"}
    # PII rule: only the distilled action/owner/due cross — nothing else.
    assert item == {"action": "Send the recap to Marco", "owner": "Ben", "due": ""}
    assert delivered[1][2] == {"action": "Book a follow-up", "owner": "", "due": "Friday"}


def test_action_requested_payload_and_external_ref_echo(monkeypatch):
    posts: list[tuple] = []

    class FakeResponse:
        status_code = 200

    def fake_post(url, payload):
        posts.append((url, payload))
        return FakeResponse()

    monkeypatch.setattr(cedric_callback, "_post", fake_post)
    ok = cedric_callback.send_action_requested(
        {"callback_url": "https://cedric.example/cb", "external_ref": {"team": "T1"}},
        "bot_7",
        {"action": "Send the recap", "owner": "Ben", "due": "Friday"},
    )
    assert ok is True
    assert len(posts) == 1
    url, payload = posts[0]
    assert url == "https://cedric.example/cb"
    assert payload["event"] == "action.requested"
    assert payload["bot_id"] == "bot_7"
    assert payload["external_ref"] == {"team": "T1"}  # echoed for correlation
    assert payload["action"] == "Send the recap"
    assert payload["owner"] == "Ben"
    assert payload["due"] == "Friday"
    assert payload["at"]


def test_action_requested_is_single_attempt_best_effort(monkeypatch):
    attempts: list = []

    def fake_post(url, payload):
        attempts.append(payload)
        raise RuntimeError("down")

    monkeypatch.setattr(cedric_callback, "_post", fake_post)
    ok = cedric_callback.send_action_requested(
        {"callback_url": "https://cedric.example/cb"}, "bot_1", {"action": "x"}
    )
    assert ok is False
    assert len(attempts) == 1  # same discipline as session.status: no retries
    # No callback_url -> no delivery at all.
    assert cedric_callback.send_action_requested(None, "b", {"action": "x"}) is False
    assert cedric_callback.send_action_requested({}, "b", {"action": "x"}) is False


# ── (d) plain session: no webhook, but artifact + ledger still get it ──


def test_plain_session_no_webhook_but_artifact_and_ledger(
    client, recall_stubbed, monkeypatch
):
    fired: list = []
    monkeypatch.setattr(
        cedric_callback, "send_action_requested", lambda *a: fired.append(a) or True
    )

    session = store.create("bot_plain", "https://meet.example/plain", "laura")
    session.add_utterance("Priya", "Let's kick off the quarterly review.")
    out = tools.dispatch(
        "queue_action",
        {"action": "Email the pricing deck to Acme", "owner": "Priya"},
        session=session,
    )
    assert "queue" in out.lower()

    time.sleep(0.1)
    assert fired == []  # non-orchestrated: action.requested never fires

    resp = client.post("/sessions/bot_plain/end")
    assert resp.status_code == 200
    actions = resp.json()["actions"]
    assert any(a.get("item") == "Email the pricing deck to Acme" for a in actions)

    # …and the ledger recorded it as an open action (same path as any meeting).
    key = ledger.meeting_key("https://meet.example/plain")
    items = ledger.items(key)
    assert any(
        i["kind"] == "action"
        and i["item"] == "Email the pricing deck to Acme"
        and i["owner"] == "Priya"
        and i["status"] == "open"
        for i in items
    )


# ── (e) the wire artifact stays transcript-free ────────────────────────


def test_wire_artifact_still_transcript_free(client, recall_stubbed, monkeypatch):
    delivered: list[dict] = []
    monkeypatch.setattr(
        cedric_callback,
        "send_ended",
        lambda integration, bot_id, artifact: delivered.append(artifact) or True,
    )
    monkeypatch.setattr(cedric_callback, "send_action_requested", lambda *a: True)

    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    session = store.get(bot_id)
    session.add_utterance("Ben", "Cedric, can you send the recap to Marco?")
    tools.dispatch(
        "queue_action", {"action": "Send the recap to Marco"}, session=session
    )

    resp = client.post(f"/sessions/{bot_id}/end")
    wire = resp.json()
    assert "transcript" not in wire
    assert wire["artifact_version"] == 1
    assert any(a.get("requested_live") for a in wire["actions"])

    assert _wait_until(lambda: len(delivered) == 1)
    assert "transcript" not in delivered[0]
    assert any(a.get("requested_live") for a in delivered[0]["actions"])

    # The full artifact (transcript included) still lives in the LOCAL store
    # only — the PII boundary is the orchestrator API, not disk.
    assert "transcript" in store.get_artifact(bot_id)
