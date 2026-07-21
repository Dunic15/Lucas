"""Built-in Cedric for the dashboard chat (cedric/chat_responder.py).

When no external events door is configured, the brain provider answers chat
messages AS Cedric; grounded in org metadata, never transcripts, never
executing anything. Key-free like the rest of the suite (stub provider =
deterministic canned reply).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.dashboard as dashboard_module
import app.main as main_module
from app import ledger, store
from app.cedric import callback as cedric_callback
from app.cedric import chat_responder
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


@pytest.fixture
def eager_threadpool(monkeypatch):
    """Run dashboard run_in_threadpool targets synchronously so the
    fire-and-forget native reply lands before the response is asserted
    (same pattern as the clarify-loop tests)."""

    def _eager(fn, *a, **k):
        result = fn(*a, **k)

        async def _done():
            return result

        return _done()

    monkeypatch.setattr(dashboard_module, "run_in_threadpool", _eager)


# ───────────────────────── the responder itself ─────────────────────────
def test_stub_reply_stores_cedric_row(client):
    assert chat_responder.respond_and_store("org_x", "what's pending?") is True
    row = store.list_chat_messages("org_x")[-1]
    assert row["sender"] == "cedric" and row["sender_label"] == "Cedric"
    assert row["body"].startswith(chat_responder._STUB_PREFIX)


def test_generation_failure_stores_apology_not_silence(client, monkeypatch):
    monkeypatch.setattr(
        chat_responder, "build_reply",
        lambda org, text: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert chat_responder.respond_and_store("org_x", "hi") is True
    assert "error" in store.list_chat_messages("org_x")[-1]["body"].lower()


def test_llm_prompt_is_grounded_and_pii_safe(client, monkeypatch):
    """Real-provider path: the system prompt carries persona + org state
    (action items, connection state) and NEVER the meeting transcript."""
    store.save_artifact(
        "bot_ctx",
        {
            "summary": "Kickoff for the rebrand.",
            "actions": [{"item": "Email the recap to Marco", "owner": "Ben",
                         "action_id": "act_ctx1"}],
            "org_id": "org_ctx",
            "avatar_id": "laura",
            "transcript": "SECRET-TRANSCRIPT-PII",
        },
        org_id="org_ctx",
    )
    store.add_chat_message("org_ctx", "user", body="earlier question")
    captured: dict = {}

    def fake_complete(system, user, **kw):
        captured["system"] = system
        captured["user"] = user
        return "On it — one action is waiting on Ben."

    monkeypatch.setattr(settings, "brain_provider", "groq")
    import app.llm as llm_module

    monkeypatch.setattr(llm_module, "complete", fake_complete)
    assert chat_responder.respond_and_store("org_ctx", "what's open?") is True

    sys_prompt = captured["system"]
    assert "Email the recap to Marco" in sys_prompt
    assert "Cedric" in sys_prompt  # persona present
    assert "Connections:" in sys_prompt
    assert "SECRET-TRANSCRIPT-PII" not in sys_prompt
    assert "SECRET-TRANSCRIPT-PII" not in captured["user"]
    assert "earlier question" in captured["user"]  # history continuity
    assert store.list_chat_messages("org_ctx")[-1]["body"].startswith("On it")


# ───────────────────────── endpoint wiring ─────────────────────────
def test_post_native_reply_when_no_relay(client, eager_threadpool, monkeypatch):
    monkeypatch.setattr(cedric_callback, "send_action_event", lambda *a: False)
    resp = client.post("/dashboard/chat", json={"text": "hi Cedric"})
    assert resp.status_code == 200
    assert resp.json()["native_reply"] is True
    msgs = store.list_chat_messages(settings.demo_org_id)
    assert [m["sender"] for m in msgs[-2:]] == ["user", "cedric"]


def test_native_answers_even_with_events_door_configured(
    client, eager_threadpool, monkeypatch
):
    """The prod bug (2026-07-20): CEDRIC_ORGS_URL is set for approvals/linking,
    so events_url() is truthy; but that must NOT suppress the built-in chat
    responder (no external Cedric answers chat). With native on, Cedric still
    replies and nothing is relayed into the void."""
    monkeypatch.setattr(
        cedric_callback, "events_url", lambda: "https://c.example/api/laura/events"
    )
    relayed: list = []
    monkeypatch.setattr(
        cedric_callback, "send_action_event",
        lambda *a: relayed.append(a) or True,
    )
    resp = client.post("/dashboard/chat", json={"text": "what's open?"})
    assert resp.status_code == 200
    assert resp.json()["native_reply"] is True
    assert relayed == []  # native owns it; no relay into a non-answering door
    senders = [m["sender"] for m in store.list_chat_messages(settings.demo_org_id)]
    assert senders == ["user", "cedric"]


def test_external_owns_reply_when_native_off(client, eager_threadpool, monkeypatch):
    """Native explicitly OFF = a real external Cedric chat runtime exists, so
    the message relays to it and the built-in responder stays silent."""
    monkeypatch.setattr(settings, "cedric_chat_native_reply", False)
    monkeypatch.setattr(
        cedric_callback, "events_url", lambda: "https://c.example/api/laura/events"
    )
    monkeypatch.setattr(cedric_callback, "send_action_event", lambda *a: True)
    resp = client.post("/dashboard/chat", json={"text": "hi"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["native_reply"] is False and body["delivered"] is True
    senders = [m["sender"] for m in store.list_chat_messages(settings.demo_org_id)]
    assert senders == ["user"]  # the external Cedric owns the reply


def test_native_off_restores_nobody_answers(client, eager_threadpool, monkeypatch):
    monkeypatch.setattr(settings, "cedric_chat_native_reply", False)
    monkeypatch.setattr(cedric_callback, "send_action_event", lambda *a: False)
    resp = client.post("/dashboard/chat", json={"text": "hello?"})
    assert resp.json()["native_reply"] is False
    assert [m["sender"] for m in store.list_chat_messages(settings.demo_org_id)] == ["user"]
    listing = client.get("/dashboard/chat").json()
    assert listing["relay_configured"] is False  # banner: nobody answers


def test_listing_reports_native_answering(client):
    listing = client.get("/dashboard/chat").json()
    assert listing["relay_configured"] is True  # built-in Cedric answers
    assert listing["native_chat"] is True
