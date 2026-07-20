"""Auto-finalize backstop: the reconciliation loop + the terminal-status webhook
+ the concurrency guard that stops a double bot.done/bot.call_ended from firing
session.ended to Cedric twice. Key-free — Recall/Anam/brain all stubbed."""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, ledger, store  # noqa: E402
from app.meeting import lifecycle  # noqa: E402  (lifecycle hoisted from main)
from app.config import settings  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200):
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _bot(code: str) -> dict:
    return {"id": "bot_x", "status_changes": [{"code": code}]}


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """A clean, isolated sqlite store + no leftover in-flight/miss state."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    monkeypatch.setattr(settings, "recall_api_base", "https://eu-central-1.recall.ai")
    main._finalizing.clear()
    main._reconcile_missing.clear()
    yield
    main._finalizing.clear()
    main._reconcile_missing.clear()


def _stub_finalize_vendors(monkeypatch):
    """Everything _finalize_session touches on the outside, stubbed offline."""
    monkeypatch.setattr(main.recall_client, "leave_call", lambda b: None)
    monkeypatch.setattr(main.anam_client, "end_conversation", lambda c: None)
    monkeypatch.setattr(
        lifecycle, "post_meeting",
        lambda avatar, transcript, **kw: {"summary": "s", "actions": [], "checklist": []},
    )
    monkeypatch.setattr(main.ledger, "record_meeting", lambda *a, **k: None)


def _session(bot_id: str = "bot_x", integration: dict | None = None) -> store.Session:
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "cedric")
    store.register_recall_realtime_capability(bot_id, "test-realtime-cap")
    s.memory_brief = ""  # skip lazy ledger load
    if integration is not None:
        s.integration = integration
    return s


# ── reconciliation loop: terminal status → finalize ──


def test_reconcile_finalizes_done_bot(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    _session()
    monkeypatch.setattr(main.httpx, "get", lambda url, *, headers, timeout: _FakeResponse(_bot("done")))

    asyncio.run(main._reconcile_once())

    assert store.get("bot_x") is None                # finalized + removed
    assert store.get_artifact("bot_x") is not None    # artifact built


def test_reconcile_finalizes_fatal_bot_and_notifies(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    _session()
    monkeypatch.setattr(main.httpx, "get", lambda url, *, headers, timeout: _FakeResponse(_bot("fatal")))
    notified = []
    monkeypatch.setattr(main.cedric, "notify_failed",
                        lambda s, b, code: notified.append((b, code)))

    asyncio.run(main._reconcile_once())

    assert store.get("bot_x") is None
    assert notified == [("bot_x", "fatal")]           # Cedric hears the join failed


def test_reconcile_skips_live_bot(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    _session()
    monkeypatch.setattr(main.httpx, "get",
                        lambda url, *, headers, timeout: _FakeResponse(_bot("in_call_recording")))

    asyncio.run(main._reconcile_once())

    assert store.get("bot_x") is not None             # still live, not finalized
    store.remove("bot_x")


def test_reconcile_404_needs_three_consecutive_misses(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    _session()
    monkeypatch.setattr(main.httpx, "get",
                        lambda url, *, headers, timeout: _FakeResponse(status_code=404))

    asyncio.run(main._reconcile_once())
    assert store.get("bot_x") is not None             # miss 1 — don't finalize
    asyncio.run(main._reconcile_once())
    assert store.get("bot_x") is not None             # miss 2 — still not
    asyncio.run(main._reconcile_once())
    assert store.get("bot_x") is None                 # miss 3 — gone, finalize


def test_reconcile_404_counter_resets_when_reachable_again(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    _session()
    responses = [_FakeResponse(status_code=404), _FakeResponse(status_code=404),
                 _FakeResponse(_bot("in_call_recording"))]  # transient blip, then back
    monkeypatch.setattr(main.httpx, "get",
                        lambda url, *, headers, timeout: responses.pop(0))

    for _ in range(3):
        asyncio.run(main._reconcile_once())
    assert store.get("bot_x") is not None             # counter reset — no false finalize
    assert main._reconcile_missing.get("bot_x") is None
    store.remove("bot_x")


def test_reconcile_hands_off_to_cedric(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    _session(integration={"callback_url": "https://meet-cedric.com/api/laura/events"})
    monkeypatch.setattr(main.httpx, "get", lambda url, *, headers, timeout: _FakeResponse(_bot("done")))
    delivered = []
    monkeypatch.setattr(main.cedric, "deliver_ended",
                        lambda integ, bot, art: delivered.append(bot) or True)

    asyncio.run(main._reconcile_once())

    assert delivered == ["bot_x"]                     # session.ended handed to Cedric


# ── concurrency guard: two terminal events must deliver ONCE ──


def test_concurrent_finalize_delivers_session_ended_once(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    s = _session(integration={"callback_url": "https://cb/events"})
    s.add_utterance("Ben", "Let's ship it.")
    delivered = []
    monkeypatch.setattr(main.cedric, "deliver_ended",
                        lambda integ, bot, art: delivered.append(bot) or True)

    async def race():
        # bot.call_ended and bot.done arrive as two concurrent webhook POSTs.
        return await asyncio.gather(
            main._finalize_session("bot_x", source="webhook"),
            main._finalize_session("bot_x", source="webhook"),
        )

    asyncio.run(race())

    assert delivered == ["bot_x"]                     # exactly one hand-off, not two
    assert store.get("bot_x") is None


# ── terminal-status webhook: account (Svix) shape with data.data.code ──


def _webhook(payload: dict) -> dict:
    class FakeRequest:
        headers: dict = {}
        query_params: dict = {"cap": "test-realtime-cap"}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    resp = asyncio.run(main.recall_webhook(FakeRequest()))
    return json.loads(resp.body)


def test_webhook_bot_fatal_finalizes_and_notifies(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    _session()
    notified = []
    monkeypatch.setattr(main.cedric, "notify_failed",
                        lambda s, b, code: notified.append((b, code)))

    body = _webhook({
        "event": "bot.fatal",
        "data": {"data": {"code": "fatal"}, "bot": {"id": "bot_x"}},
    })

    assert body.get("finalized") == "bot_x"
    assert store.get("bot_x") is None
    assert notified == [("bot_x", "fatal")]           # was silently dropped before the fix


def test_webhook_bot_done_finalizes(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    _session()

    body = _webhook({
        "event": "bot.done",
        "data": {"data": {"code": "done"}, "bot": {"id": "bot_x"}},
    })

    assert body.get("finalized") == "bot_x"
    assert store.get("bot_x") is None


def test_webhook_short_code_only_finalizes(fresh_store, monkeypatch):
    # A payload whose event isn't a bot.* name but carries the short code at
    # data.data.code must still finalize (data.data.code is now read).
    _stub_finalize_vendors(monkeypatch)
    _session()

    body = _webhook({
        "event": "bot.status_change",
        "data": {"data": {"code": "call_ended"}, "bot": {"id": "bot_x"}},
    })

    assert body.get("finalized") == "bot_x"
    assert store.get("bot_x") is None


def test_webhook_done_does_not_notify_failed(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    _session()
    notified = []
    monkeypatch.setattr(main.cedric, "notify_failed",
                        lambda s, b, code: notified.append(code))

    _webhook({"event": "bot.done", "data": {"data": {"code": "done"}, "bot": {"id": "bot_x"}}})

    assert notified == []                             # 'done' is not a failure


# ── review-fix A: 404 for a cancelled/no-show bot must NOT fire session.ended ──


def test_reconcile_404_empty_abandons_without_delivery(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    _session(integration={"callback_url": "https://cb/events"})  # Model A wired
    monkeypatch.setattr(main.httpx, "get",
                        lambda url, *, headers, timeout: _FakeResponse(status_code=404))
    delivered = []
    monkeypatch.setattr(main.cedric, "deliver_ended",
                        lambda integ, bot, art: delivered.append(bot) or True)

    for _ in range(3):
        asyncio.run(main._reconcile_once())

    assert store.get("bot_x") is None                 # orphan dropped
    assert delivered == []                            # NO phantom session.ended
    assert store.get_artifact("bot_x") is None        # no artifact for a no-show


def test_abandon_defers_to_in_flight_finalize(fresh_store):
    # Guard safety: if a real finalize already owns the bot, abandon must no-op
    # (not race past store.remove and let a phantom session.ended slip out).
    _session("bot_orphan")
    main._finalizing.add("bot_orphan")                # a concurrent finalize holds it
    try:
        asyncio.run(main._abandon_orphan_session("bot_orphan"))
    finally:
        main._finalizing.discard("bot_orphan")
    assert store.get("bot_orphan") is not None        # abandon deferred, didn't remove
    store.remove("bot_orphan")


def test_reconcile_404_with_transcript_finalizes_and_delivers(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    s = _session(integration={"callback_url": "https://cb/events"})
    s.add_utterance("Ben", "Real meeting content.")   # it WAS live and captured
    monkeypatch.setattr(main.httpx, "get",
                        lambda url, *, headers, timeout: _FakeResponse(status_code=404))
    delivered = []
    monkeypatch.setattr(main.cedric, "deliver_ended",
                        lambda integ, bot, art: delivered.append(bot) or True)

    for _ in range(3):
        asyncio.run(main._reconcile_once())

    assert store.get("bot_x") is None
    assert delivered == ["bot_x"]                     # real meeting -> deliver


# ── review-fix C: fatal notify_failed fires ONCE under the guard ──


def test_concurrent_fatal_notifies_failed_once(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    s = _session(integration={"callback_url": "https://cb/events"})
    s.add_utterance("Ben", "hi")
    notified = []
    monkeypatch.setattr(main.cedric, "notify_failed",
                        lambda s, b, code: notified.append(code))

    async def race():
        return await asyncio.gather(
            main._finalize_session("bot_x", source="webhook", failed_code="fatal"),
            main._finalize_session("bot_x", source="reconcile", failed_code="fatal"),
        )

    asyncio.run(race())

    assert notified == ["fatal"]                      # guarded -> exactly one


# ── review-fix D: miss-counter is pruned for bots no longer in the store ──


def test_reconcile_prunes_missing_for_removed_bots(fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    main._reconcile_missing["ghost"] = 2              # leftover from a finalized bot
    _session()
    monkeypatch.setattr(main.httpx, "get",
                        lambda url, *, headers, timeout: _FakeResponse(_bot("in_call_recording")))

    asyncio.run(main._reconcile_once())

    assert "ghost" not in main._reconcile_missing     # pruned
    store.remove("bot_x")


# ── review-fix B: /end answers 202 (not 404) while a finalize is in flight ──


def test_end_returns_202_when_finalize_in_flight(fresh_store):
    from fastapi.testclient import TestClient
    _session("bot_inflight")
    main._finalizing.add("bot_inflight")              # a concurrent finalize holds the guard
    try:
        resp = TestClient(main.app).post("/sessions/bot_inflight/end")
    finally:
        main._finalizing.discard("bot_inflight")
    assert resp.status_code == 202
    assert resp.json().get("finalizing") == "bot_inflight"
    assert store.get("bot_inflight") is not None      # the real finalize still owns it
    store.remove("bot_inflight")


def test_end_unknown_bot_still_404(fresh_store):
    from fastapi.testclient import TestClient
    resp = TestClient(main.app).post("/sessions/nope/end")
    assert resp.status_code == 404
