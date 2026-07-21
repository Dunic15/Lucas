"""A terminal Recall bot (done/call_ended/fatal) is NOT billing, so a failing
leave_call on it must NOT strand the session as leave_pending; the phantom
active_sessions that blocks the pre-deploy gate.

Repro for the demo-eve phantom: a naturally-ended meeting is caught by the
reconcile terminal-status poll (or the terminal webhook); finalize then calls
leave_call as a courtesy, but Recall answers 400 "bot is not in a call" for an
already-ended bot. Before the fix that 400 was classified UNVERIFIED, so the
session was kept + retried forever (active_sessions never returns to 0). After
the fix, a bot_terminal finalize treats the meter as already off and drops the
session cleanly.

Key-free: Recall/Anam/brain all stubbed; control plane disabled (no metering).
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, ledger, store  # noqa: E402
from app.meeting import lifecycle  # noqa: E402  (lifecycle hoisted from main)
from app.config import settings  # noqa: E402


_MEET_URL = "https://meet.google.com/abc-defg-hij"


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://recall.test/leave")
    return httpx.HTTPStatusError(
        f"status {status_code}", request=req,
        response=httpx.Response(status_code, request=req),
    )


class _FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200):
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _bot(code: str, bot_id: str = "bot_x") -> dict:
    return {"id": bot_id, "meeting_url": _MEET_URL, "status_changes": [{"code": code}]}


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
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


def _stub_offline(monkeypatch, delivered=None):
    monkeypatch.setattr(main.anam_client, "end_conversation", lambda c: None)
    monkeypatch.setattr(
        lifecycle, "post_meeting",
        lambda avatar, transcript, **kw: {"summary": "s", "actions": [], "checklist": []},
    )
    monkeypatch.setattr(main.ledger, "record_meeting", lambda *a, **k: None)
    monkeypatch.setattr(
        main.cedric, "deliver_ended",
        lambda integ, bot, art: (delivered.append(bot) if delivered is not None else None) or True,
    )


def _session(bot_id="bot_x", line="Let's ship on Friday."):
    s = store.create(bot_id, _MEET_URL, "laura")
    s.memory_brief = ""
    s.add_utterance("Ben", line)
    return s


# ── reconcile terminal-status poll: leave_call 400 must still DROP the bot ──

def test_reconcile_terminal_bot_dropped_even_if_leave_400(fresh_store, monkeypatch):
    _stub_offline(monkeypatch)
    _session("bot_x")
    # Recall GET reports the bot terminal (meeting ended); leave_call answers 400
    # "bot is not in a call" (already ended); must not strand as leave_pending.
    monkeypatch.setattr(main.httpx, "get",
                        lambda url, *, headers, timeout: _FakeResponse(_bot("done")))
    monkeypatch.setattr(main.recall_client, "leave_call",
                        lambda bot_id: (_ for _ in ()).throw(_http_status_error(400)))

    asyncio.run(main._reconcile_once())

    kept = store.get("bot_x")
    assert kept is None, "terminal bot stranded as phantom on a 400 leave_call"


# ── terminal webhook (bot.done) with a 400 leave_call must DROP too ──

def test_finalize_terminal_flag_drops_on_400(fresh_store, monkeypatch):
    _stub_offline(monkeypatch)
    _session("bot_x")
    monkeypatch.setattr(main.recall_client, "leave_call",
                        lambda bot_id: (_ for _ in ()).throw(_http_status_error(400)))

    # bot_terminal=True is what the reconcile-terminal + webhook-terminal callers pass.
    asyncio.run(main._finalize_session("bot_x", source="webhook", bot_terminal=True))

    assert store.get("bot_x") is None
    assert store.get_artifact("bot_x") is not None  # deliverable still built + saved


# ── regression: a possibly-LIVE bot (manual /end, not terminal) still KEPT ──

def test_manual_end_live_bot_still_kept_on_unverified_leave(fresh_store, monkeypatch):
    # bot_terminal defaults False: a 401/429/5xx (or 400) on a bot that may still
    # be live must stay UNVERIFIED; keep the session so reconcile retries. The
    # fleet meter-leak guard must be untouched by the terminal fast-path.
    _stub_offline(monkeypatch)
    _session("bot_x")
    monkeypatch.setattr(main.recall_client, "leave_call",
                        lambda bot_id: (_ for _ in ()).throw(_http_status_error(429)))

    asyncio.run(main._finalize_session("bot_x", source="manual"))

    kept = store.get("bot_x")
    assert kept is not None
    assert getattr(kept, "leave_pending") is True


# ── restored leave_pending phantom: _retry_leave must DRAIN a terminal bot ──
# leave_pending IS persisted (SQLite → S3), so a phantom reappears after a
# redeploy and lands in _retry_leave. A 400 "not in a call" is NOT a gone-status,
# so without the status-poll drain it retries forever (the observed reappearance).

def test_retry_leave_drains_terminal_bot_on_400(fresh_store, monkeypatch):
    s = _session("bot_x")
    s.leave_pending = True
    monkeypatch.setattr(main.recall_client, "leave_call",
                        lambda bot_id: (_ for _ in ()).throw(_http_status_error(400)))
    # Recall reports the bot terminal → not billing → drain, don't retry forever.
    monkeypatch.setattr(main.httpx, "get",
                        lambda url, *, headers, timeout: _FakeResponse(_bot("done")))

    assert asyncio.run(main._retry_leave("bot_x", s)) is True
    assert store.get("bot_x") is None


def test_retry_leave_drains_when_status_poll_404(fresh_store, monkeypatch):
    # A 429 leave (auth/rate; not a gone-status) but the status poll 404s → the
    # bot is gone → not billing → drain.
    s = _session("bot_x")
    s.leave_pending = True
    monkeypatch.setattr(main.recall_client, "leave_call",
                        lambda bot_id: (_ for _ in ()).throw(_http_status_error(429)))
    monkeypatch.setattr(main.httpx, "get",
                        lambda url, *, headers, timeout: _FakeResponse(status_code=404))

    assert asyncio.run(main._retry_leave("bot_x", s)) is True
    assert store.get("bot_x") is None


def test_retry_leave_keeps_when_bot_still_live_on_400(fresh_store, monkeypatch):
    # Safety: a 400 leave but the bot is STILL in call per the poll → possibly
    # billing → keep (never drop a live bot on an ambiguous leave).
    s = _session("bot_x")
    s.leave_pending = True
    monkeypatch.setattr(main.recall_client, "leave_call",
                        lambda bot_id: (_ for _ in ()).throw(_http_status_error(400)))
    monkeypatch.setattr(main.httpx, "get",
                        lambda url, *, headers, timeout: _FakeResponse(_bot("in_call_recording")))

    assert asyncio.run(main._retry_leave("bot_x", s)) is False
    assert store.get("bot_x") is not None


def test_restored_leave_pending_phantom_drains_via_reconcile(fresh_store, monkeypatch):
    # End-to-end reappearance: a persisted leave_pending session (as restored from
    # S3 after a redeploy) whose bot is terminal must drain on the reconcile pass,
    # not linger as active_sessions forever.
    _stub_offline(monkeypatch)
    s = _session("bot_x")
    s.leave_pending = True  # what the SQLite restore sets on a phantom
    monkeypatch.setattr(main.recall_client, "leave_call",
                        lambda bot_id: (_ for _ in ()).throw(_http_status_error(400)))
    monkeypatch.setattr(main.httpx, "get",
                        lambda url, *, headers, timeout: _FakeResponse(_bot("done")))

    asyncio.run(main._reconcile_once())

    assert store.get("bot_x") is None, "restored leave_pending phantom not drained"
