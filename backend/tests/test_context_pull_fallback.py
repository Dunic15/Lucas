"""Fallback pre-meeting context pull (Cedric -> Laura).

Production finding 2026-07-10: Recall's realtime webhook delivered NO
bot-status events (every finalize was source=reconcile), so the "live"-status
trigger in handle_webhook_status never fired and the avatar joined meetings
without Cedric's brief. The fallback: the FIRST transcript webhook of an
orchestrated session (partial or final) launches the same one-shot refresh.

Also covers the routing hints on the context GET: external_ref.team /
slack_channel ride the URL as ?team=&channel= (Cedric's endpoint is
"No team => empty brief").

Key-free like the rest of the suite: fetch_context is stubbed (or served by an
httpx.MockTransport); webhooks are posted through an in-process ASGI transport
on a private event loop, so the fire-and-forget refresh task can be drained
deterministically before asserting (no sleeps).
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main as main_module
from app import cedric, ledger, store
from app.cedric import callback as cedric_callback
from app.config import settings


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    # Reload mutates the module objects in place, so main's `from . import
    # store, ledger` references follow automatically (same as the other suites).
    importlib.reload(store)
    importlib.reload(ledger)
    return store


CONTEXT_URL = "https://cedric.example/api/laura/context"


def _orchestrated(store_mod, bot_id: str, context_url: str = CONTEXT_URL):
    s = store_mod.create(
        bot_id=bot_id,
        meeting_url=f"https://meet.google.com/{bot_id}",
        avatar_id="cedric",
    )
    s.integration = {
        "callback_url": "",
        "context_url": context_url,
        "external_ref": {},
        "brief": "stale booking-time brief",
        "meeting": {},
        "context_refreshed": False,
    }
    return s


def _transcript_payload(event: str, bot_id: str, text: str = "hello there everyone"):
    return {
        "event": event,
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "words": [{"text": t} for t in text.split()],
                "participant": {"id": 7, "name": "Ben"},
            },
        },
    }


def _status_payload(bot_id: str, code: str = "in_call_recording"):
    return {
        "event": "bot.status_change",
        "data": {"bot": {"id": bot_id}, "status": {"code": code}},
    }


def _post_webhook(*payloads: dict) -> list[httpx.Response]:
    """POST payloads to /webhooks/recall in-process, then drain every task the
    handlers spawned (the fire-and-forget refresh) before returning — so the
    caller asserts on a settled world, deterministically."""

    async def _run() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=main_module.app)
        responses = []
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            for payload in payloads:
                responses.append(await ac.post("/webhooks/recall", json=payload))
                pending = asyncio.all_tasks() - {asyncio.current_task()}
                if pending:
                    await asyncio.wait(pending, timeout=2.0)
        return responses

    return asyncio.run(_run())


@pytest.fixture
def fetch_calls(monkeypatch):
    """Stub cedric_callback.fetch_context with a call recorder returning a
    fresh context."""
    calls: list[dict] = []

    def fake_fetch(integration):
        calls.append(dict(integration or {}))
        return {"meeting": {"title": "Q3 sync"}, "brief_markdown": "FRESH BRIEF"}

    monkeypatch.setattr(cedric_callback, "fetch_context", fake_fetch)
    return calls


# ── trigger: first transcript webhook, no status webhook ever ──


def test_first_partial_pulls_context_without_status_webhook(fresh_store, fetch_calls):
    session = _orchestrated(fresh_store, "bot_p1")
    (resp,) = _post_webhook(_transcript_payload("transcript.partial_data", "bot_p1"))
    assert resp.status_code == 200
    assert len(fetch_calls) == 1
    assert session.integration["brief"] == "FRESH BRIEF"
    assert session.integration["meeting"] == {"title": "Q3 sync"}
    assert session.integration["context_refreshed"] is True


def test_first_final_transcript_also_triggers(fresh_store, fetch_calls):
    # The very first webhook can be a final (no partials configured/delivered).
    session = _orchestrated(fresh_store, "bot_f1")
    (resp,) = _post_webhook(_transcript_payload("transcript.data", "bot_f1"))
    assert resp.status_code == 200
    assert len(fetch_calls) == 1
    assert session.integration["brief"] == "FRESH BRIEF"


def test_second_transcript_does_not_refetch(fresh_store, fetch_calls):
    _orchestrated(fresh_store, "bot_p2")
    _post_webhook(
        _transcript_payload("transcript.partial_data", "bot_p2"),
        _transcript_payload("transcript.data", "bot_p2"),
        _transcript_payload("transcript.data", "bot_p2", text="and another line"),
    )
    assert len(fetch_calls) == 1  # flag flipped before the task launched


def test_non_orchestrated_session_never_fetches(fresh_store, fetch_calls):
    fresh_store.create(
        bot_id="bot_plain",
        meeting_url="https://meet.google.com/bot_plain",
        avatar_id="cedric",
    )  # integration stays None
    _post_webhook(
        _transcript_payload("transcript.partial_data", "bot_plain"),
        _transcript_payload("transcript.data", "bot_plain"),
    )
    assert fetch_calls == []


def test_orchestrated_without_context_url_never_fetches(fresh_store, fetch_calls):
    # callback_url-only wiring (Model A events, no context pull configured).
    s = _orchestrated(fresh_store, "bot_nocurl", context_url="")
    s.integration = {**s.integration, "callback_url": "https://cedric.example/cb"}
    _post_webhook(_transcript_payload("transcript.data", "bot_nocurl"))
    assert fetch_calls == []


def test_status_webhook_still_triggers_and_transcript_does_not_double(
    fresh_store, fetch_calls
):
    # The original trigger keeps working through the shared function, and the
    # transcript fallback sees the flag — one fetch total.
    session = _orchestrated(fresh_store, "bot_s1")
    _post_webhook(
        _status_payload("bot_s1"),
        _transcript_payload("transcript.partial_data", "bot_s1"),
    )
    assert len(fetch_calls) == 1
    assert session.integration["brief"] == "FRESH BRIEF"


def test_failed_fetch_keeps_booking_brief_and_does_not_retry(fresh_store, monkeypatch):
    # A refresh failure is best-effort: the booking-time brief survives and the
    # flag still guards (no retry storm on every transcript line).
    calls: list[dict] = []
    monkeypatch.setattr(
        cedric_callback, "fetch_context", lambda integ: calls.append(integ) or None
    )
    session = _orchestrated(fresh_store, "bot_fail")
    _post_webhook(
        _transcript_payload("transcript.partial_data", "bot_fail"),
        _transcript_payload("transcript.data", "bot_fail"),
    )
    assert len(calls) == 1
    assert session.integration["brief"] == "stale booking-time brief"
    assert session.integration["context_refreshed"] is True


# ── routing hints on the context GET URL ──


def test_context_url_gains_team_and_channel_params():
    url = cedric_callback._context_request_url(
        {
            "context_url": CONTEXT_URL,
            "org_id": "org_customer",
            "external_ref": {"team": "T1", "slack_channel": "#cedric"},
        }
    )
    assert url == f"{CONTEXT_URL}?team=T1&channel=%23cedric"


def test_context_url_preserves_existing_query():
    url = cedric_callback._context_request_url(
        {
            "context_url": f"{CONTEXT_URL}?avatar=cedric",
            "external_ref": {"team": "T1", "slack_channel": "#ops"},
        }
    )
    assert url == f"{CONTEXT_URL}?avatar=cedric&team=T1&channel=%23ops"


def test_context_url_configured_params_win_over_external_ref():
    # An explicit team= in the configured URL is deliberate ops wiring; the
    # session's external_ref must not duplicate or override it.
    url = cedric_callback._context_request_url(
        {
            "context_url": f"{CONTEXT_URL}?team=T9",
            "external_ref": {"team": "T1", "slack_channel": "#ops"},
        }
    )
    assert url == f"{CONTEXT_URL}?team=T9&channel=%23ops"


def test_context_url_unchanged_without_routing_ref():
    plain = f"{CONTEXT_URL}?a=1&b=two%20words"
    for ref in ({}, {"requested_by": "duccio"}, ["not", "a", "dict"], None):
        got = cedric_callback._context_request_url(
            {"context_url": plain, "external_ref": ref}
        )
        assert got == plain  # byte-for-byte: nothing to append


def test_fetch_context_sends_routing_params(monkeypatch):
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        cedric_callback.secret_registry,
        "bearer_for",
        lambda org: "workspace-token" if org == "org_customer" else "",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(
            200, json={"context": {"meeting": {}, "brief_markdown": "B"}}
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
    )

    ctx = cedric_callback.fetch_context(
        {
            "context_url": CONTEXT_URL,
            "org_id": "org_customer",
            "external_ref": {"team": "T1", "slack_channel": "#cedric"},
        }
    )
    assert ctx == {"meeting": {}, "brief_markdown": "B"}
    assert seen == [
        (f"{CONTEXT_URL}?team=T1&channel=%23cedric", "Bearer workspace-token")
    ]


# ── per-session authentication on unsigned Recall realtime events ──


def test_recall_realtime_capability_rejects_missing_wrong_and_cross_session(
    fresh_store, monkeypatch
):
    """Production rejects before parsing, and one bot's URL cannot mutate another."""
    monkeypatch.setattr(settings, "recall_api_key", "prod-recall-key")
    bot_a = fresh_store.create(
        "bot_cap_a", "https://meet.google.com/cap-a", "cedric"
    )
    bot_b = fresh_store.create(
        "bot_cap_b", "https://meet.google.com/cap-b", "cedric"
    )
    assert fresh_store.register_recall_realtime_capability("bot_cap_a", "cap-a")
    assert fresh_store.register_recall_realtime_capability("bot_cap_b", "cap-b")

    joined_b = {
        "event": "participant_events.join",
        "data": {
            "bot": {"id": "bot_cap_b"},
            "data": {"participant": {"id": "p1", "name": "Alice"}},
        },
    }

    async def _run():
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            missing = await ac.post("/webhooks/recall", content=b"{not-json")
            wrong = await ac.post(
                "/webhooks/recall?cap=wrong", content=b"{not-json"
            )
            cross = await ac.post(
                "/webhooks/recall?cap=cap-a", json=joined_b
            )
            # Cross-session rejection happened before any roster side effect.
            assert bot_a.participants == {}
            assert bot_b.participants == {}
            valid = await ac.post(
                "/webhooks/recall?cap=cap-b", json=joined_b
            )
            return missing, wrong, cross, valid

    missing, wrong, cross, valid = asyncio.run(_run())
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert cross.status_code == 403
    assert valid.status_code == 200
    assert bot_b.participants["p1"]["name"] == "Alice"


def test_recall_capability_is_deleted_with_session(fresh_store):
    fresh_store.create("bot_cap_cleanup", "https://meet.google.com/cap-c", "cedric")
    assert fresh_store.register_recall_realtime_capability(
        "bot_cap_cleanup", "cap-cleanup"
    )
    assert (
        fresh_store.resolve_recall_realtime_capability("cap-cleanup")
        == "bot_cap_cleanup"
    )
    fresh_store.remove("bot_cap_cleanup")
    assert fresh_store.resolve_recall_realtime_capability("cap-cleanup") is None
