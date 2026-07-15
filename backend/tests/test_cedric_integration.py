"""Cedric-integration surface: StartRequest fields, API auth, callbacks, cancel.

Key-free like the rest of the suite: no vendors are called — recall/anam are
monkeypatched, callbacks hit a local capture, the brain stays stub.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import ledger, store
from app.cedric import callback as cedric_callback
from app.config import settings


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

    def fake_create_bot(meeting_url, avatar_page_url, join_at=None, bot_name="Laura", avatar_id=""):
        created.append(
            {
                "meeting_url": meeting_url,
                "join_at": join_at,
                "bot_name": bot_name,
            }
        )
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


def test_start_session_carries_integration_and_bot_name(client, recall_stubbed):
    resp = client.post("/sessions/start", json=START_BODY)
    assert resp.status_code == 200, resp.text
    bot_id = resp.json()["bot_id"]

    assert recall_stubbed[0]["bot_name"] == "Cedric"  # avatar display name
    session = store.get(bot_id)
    assert session.integration["callback_url"] == START_BODY["callback_url"]
    assert session.integration["external_ref"] == {"team": "T1", "meet_session_id": "ms_1"}
    assert "Discuss Q3" in session.integration["brief"]


def test_duplicate_meeting_url_is_409(client, recall_stubbed):
    assert client.post("/sessions/start", json=START_BODY).status_code == 200
    resp = client.post("/sessions/start", json=START_BODY)
    assert resp.status_code == 409
    assert "bot_id" in resp.json()


def test_oversized_brief_is_400(client, recall_stubbed):
    body = dict(START_BODY)
    body["context"] = {"meeting": {}, "brief_markdown": "x" * (33 * 1024)}
    body["meeting_url"] = "https://meet.google.com/xyz-nope-zzz"
    assert client.post("/sessions/start", json=body).status_code == 400


def test_calendar_autojoin_carries_avatar_bot_name(client, recall_stubbed, monkeypatch):
    """Calendar-booked bots must join under the avatar's name — the echo guard
    matches speaker == avatar.name, so a 'Laura'-labelled cedric bot would
    answer its own transcribed speech (the self-conversation bug class)."""
    monkeypatch.setattr(
        main_module.recall_client, "verify_webhook", lambda body, headers: None
    )
    monkeypatch.setattr(settings, "calendar_invite_emails", "")
    event = {
        "avatar_id": "cedric",
        "raw": {
            "start": {"dateTime": "2099-07-02T15:00:00+02:00"},
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/cal-bot-name"}
                ]
            },
        },
    }

    async def fake_events(payload):
        return [event]

    monkeypatch.setattr(main_module, "_calendar_events_from_payload", fake_events)
    resp = client.post("/webhooks/recall-calendar", json={})
    assert resp.status_code == 200, resp.text
    assert recall_stubbed, resp.json()
    assert recall_stubbed[-1]["bot_name"] == "Cedric"


def test_auth_required_when_token_set(client, recall_stubbed, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "sekrit")
    assert client.post("/sessions/start", json=START_BODY).status_code == 401
    assert client.get("/ledger", params={"meeting_url": "x"}).status_code == 401
    assert client.get("/sessions/whatever/artifact").status_code == 401
    assert client.post("/sessions/whatever/redeliver").status_code == 401
    ok = client.post(
        "/sessions/start",
        json=START_BODY,
        headers={"Authorization": "Bearer sekrit"},
    )
    assert ok.status_code == 200


def test_cancel_removes_session_without_artifact(client, recall_stubbed):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    resp = client.post(f"/sessions/{bot_id}/cancel")
    assert resp.status_code == 200 and resp.json()["cancelled"] is True
    assert store.get(bot_id) is None
    assert store.get_artifact(bot_id) is None
    # idempotent-ish: cancelling again is a clean 404, artifact stays absent
    assert client.post(f"/sessions/{bot_id}/cancel").status_code == 404


def test_end_delivers_ended_callback(client, recall_stubbed, monkeypatch):
    delivered: list[tuple] = []

    # Capture on the SYNCHRONOUS delivery seam. cedric.deliver_ended runs
    # in-request at finalize and distils via wire_artifact BEFORE scheduling the
    # fire-and-forget send_ended. Mocking send_ended — the async inner — is
    # flaky under TestClient: its create_task is orphaned once the request's
    # portal closes, so `delivered` may never fill under CI load. Distil here
    # exactly as the real async path would, so the assertions are deterministic.
    def fake_deliver_ended(integration, bot_id, artifact):
        delivered.append(
            (integration, bot_id, main_module.cedric.wire_artifact(artifact))
        )
        return bool(integration and integration.get("callback_url"))

    monkeypatch.setattr(main_module.cedric, "deliver_ended", fake_deliver_ended)
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    session = store.get(bot_id)
    session.add_utterance("Ben", "Let's decide the roadmap. John owns rollout.")

    resp = client.post(f"/sessions/{bot_id}/end")
    assert resp.status_code == 200
    artifact = resp.json()
    # The wire artifact is distilled: transcripts are PII and stay home.
    assert "transcript" not in artifact
    assert "summary" in artifact
    assert artifact["artifact_version"] == 1

    assert len(delivered) == 1
    integration, delivered_bot, delivered_artifact = delivered[0]
    assert delivered_bot == bot_id
    assert integration["external_ref"]["meet_session_id"] == "ms_1"
    assert "transcript" not in delivered_artifact
    assert delivered_artifact["artifact_version"] == 1
    assert delivered_artifact["summary"] == artifact["summary"]
    # The full artifact (transcript included) is still served by the LOCAL
    # meetings archive — the PII boundary is the orchestrator API, not disk.
    stored = client.get("/meetings/list").json()["meetings"]
    assert any("transcript" in m.get("artifact", {}) for m in stored)
    # And the orchestrator's poll endpoint serves the same distilled copy.
    polled = client.get(f"/sessions/{bot_id}/artifact").json()
    assert polled["status"] == "done"
    assert "transcript" not in polled
    assert polled["artifact_version"] == 1


def test_redeliver_uses_saved_distilled_artifact(client, monkeypatch):
    captured: list[tuple] = []
    bot_id = "bot_redeliver"
    store.save_artifact(
        bot_id,
        {
            "summary": "Safe summary",
            "transcript": "private transcript",
            "org_id": "org_1",
            "actions": [],
        },
    )
    monkeypatch.setattr(
        settings, "surface_webhook_url", "https://cedric.example/api/laura/events"
    )
    # redeliver is now fire-and-forget: it hands off to cedric.deliver_ended (the
    # SYNCHRONOUS distil-then-schedule seam) and answers 202 immediately, instead
    # of awaiting send_ended's ~150s blocking retry chain and 504-ing. Capture at
    # that seam — deterministic under TestClient, unlike the orphaned async
    # send_ended (see test_end_delivers_ended_callback for why).
    def fake_deliver_ended(integration, delivered_bot, artifact):
        captured.append(
            (integration, delivered_bot, main_module.cedric.wire_artifact(artifact))
        )
        return bool(integration and integration.get("callback_url"))

    monkeypatch.setattr(main_module.cedric, "deliver_ended", fake_deliver_ended)

    resp = client.post(f"/sessions/{bot_id}/redeliver")

    assert resp.status_code == 202, resp.text
    assert resp.json().get("status") == "retrying"
    integration, delivered_bot, artifact = captured.pop()
    assert delivered_bot == bot_id
    assert integration["org_id"] == "org_1"
    assert artifact["summary"] == "Safe summary"
    assert "transcript" not in artifact  # distilled: PII stays home


def test_ended_callback_payload_signature_and_retries(monkeypatch):
    calls: list[dict] = []

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

    def fake_post(url, payload):
        calls.append(payload)
        # fail twice, succeed on the third attempt
        return FakeResponse(500 if len(calls) < 3 else 200)

    monkeypatch.setattr(cedric_callback, "_post", fake_post)
    monkeypatch.setattr(cedric_callback, "ENDED_BACKOFF", (0.0, 0.0, 0.0))

    ok = cedric_callback.send_ended(
        {"callback_url": "https://cedric.example/cb", "external_ref": {"a": 1}},
        "bot_9",
        {"summary": "s", "transcript": "t"},
    )
    assert ok is True
    assert len(calls) == 3
    payload = calls[0]
    assert payload["event"] == "session.ended"
    assert payload["bot_id"] == "bot_9"
    assert payload["external_ref"] == {"a": 1}
    assert payload["artifact"]["summary"] == "s"
    # Even when a caller hands the transport a raw artifact, PII never ships.
    assert "transcript" not in payload["artifact"]


def test_signature_headers_hmac(monkeypatch):
    monkeypatch.setattr(settings, "laura_webhook_secret", "topsecret")
    monkeypatch.setattr(settings, "laura_webhook_token", "tok")
    body = json.dumps({"x": 1}).encode()
    headers = cedric_callback._signature_headers(body)

    assert headers["Authorization"] == "Bearer tok"
    ts, v1 = None, None
    for part in headers["X-Laura-Signature"].split(","):
        key, _, value = part.partition("=")
        if key == "t":
            ts = value
        elif key == "v1":
            v1 = value
    expected = hmac.new(
        b"topsecret", f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    assert v1 == expected


def test_status_callback_is_single_attempt(monkeypatch):
    attempts = []

    def fake_post(url, payload):
        attempts.append(payload)
        raise RuntimeError("down")

    monkeypatch.setattr(cedric_callback, "_post", fake_post)
    ok = cedric_callback.send_status(
        {"callback_url": "https://cedric.example/cb"}, "bot_1", "live"
    )
    assert ok is False
    assert len(attempts) == 1


def test_post_refuses_cross_origin_redirect_with_auth(monkeypatch):
    """A redirect may never carry workspace credentials to another origin."""
    monkeypatch.setattr(settings, "laura_webhook_token", "tok")
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(
            308, headers={"location": "https://attacker.example/steal"}
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
    )

    resp = cedric_callback._post("https://cedric.example/cb", {"x": 1})
    assert resp.status_code == 308
    assert seen == [("https://cedric.example/cb", "Bearer tok")]


def test_post_follows_same_origin_redirect(monkeypatch):
    monkeypatch.setattr(settings, "laura_webhook_token", "tok")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/old":
            return httpx.Response(308, headers={"location": "/new"})
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
    )
    assert cedric_callback._post("https://cedric.example/old", {"x": 1}).status_code == 200
    assert seen == ["https://cedric.example/old", "https://cedric.example/new"]

def test_no_callback_url_means_no_delivery(monkeypatch):
    monkeypatch.setattr(
        cedric_callback, "_post", lambda *a: (_ for _ in ()).throw(AssertionError)
    )
    assert cedric_callback.send_ended(None, "b", {}) is False
    assert cedric_callback.send_status({}, "b", "live") is False


def test_integration_survives_store_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    import app.store as store_module

    store_module = importlib.reload(store_module)
    session = store_module.create("bot_p", "https://meet.example/x", "cedric")
    session.integration = {"callback_url": "https://cb", "external_ref": {"k": "v"}}

    store_module = importlib.reload(store_module)
    loaded = store_module.get("bot_p")
    assert loaded.integration == {
        "callback_url": "https://cb",
        "external_ref": {"k": "v"},
    }
