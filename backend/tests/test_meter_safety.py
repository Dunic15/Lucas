"""Money-safety (per-minute meter) regressions for three adversarial-audit findings:

  1. Manual POST /sessions/start had no durable duplicate-bot guard and a
     double-click race → two bots + two meters in one call.
  2. The durable guards keyed on a Meet-ONLY regex → Zoom/Teams silently no-op'd
     (two bots, uncleaned).
  3. leave_call swallowed a Recall 5xx → meter never stopped and the session was
     deleted so the reconcile backstop could never retry (permanent bill leak).

Key-free: Recall/Anam/brain are all stubbed; no network, no secrets.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import main, ledger, recall_client, store  # noqa: E402
from app.api import sessions as _sessions  # noqa: E402  (sessions extracted)
from app.meeting import lifecycle  # noqa: E402  (lifecycle hoisted from main)
from app.config import settings  # noqa: E402


# ── shared fixtures / helpers ───────────────────────────────────────────────

_MEET_URL = "https://meet.google.com/abc-defg-hij"
_ZOOM_URL = "https://zoom.us/j/1234567890"
_TEAMS_URL = (
    "https://teams.microsoft.com/l/meetup-join/"
    "19%3ameeting_ZjExampleThread%40thread.v2/0?context=%7b%7d"
)


class _FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200):
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _bot(
    meeting_url,
    *,
    code: str = "in_call_recording",
    bot_id: str = "bot_x",
    created_at: str = "2026-07-12T09:00:00Z",
    variant: dict | None = None,
) -> dict:
    """A Recall bot record. ``meeting_url`` may be a full URL string OR the
    structured object Recall returns ({"meeting_id": <native id>})."""
    return {
        "id": bot_id,
        "created_at": created_at,
        "variant": variant,
        "meeting_url": meeting_url,
        "status_changes": [{"code": code}],
    }


def _recall_ready(monkeypatch) -> None:
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    monkeypatch.setattr(settings, "recall_api_base", "https://eu-central-1.recall.ai")


def _dummy_request() -> httpx.Request:
    return httpx.Request("POST", "https://recall.test/leave")


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    req = _dummy_request()
    return httpx.HTTPStatusError(
        f"status {status_code}", request=req, response=httpx.Response(status_code, request=req)
    )


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Clean, isolated sqlite store + no leftover in-flight/miss state."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    main._finalizing.clear()
    main._reconcile_missing.clear()
    yield
    main._finalizing.clear()
    main._reconcile_missing.clear()


# ── Defect 2: durable guards are platform-aware (Meet/Zoom/Teams) ────────────


def test_active_bot_guard_matches_zoom_via_meeting_id(monkeypatch):
    # Recall reports Zoom as an object whose meeting_id IS the native numeric id,
    # i.e. exactly ledger.meeting_key(zoom_url). Previously the Meet-only regex
    # made our key the whole URL, so this never matched → two Zoom bots.
    _recall_ready(monkeypatch)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            {"results": [_bot({"meeting_id": "1234567890"})]}
        ),
    )
    assert main._meeting_has_active_bot(_ZOOM_URL) is True


def test_active_bot_guard_matches_teams_via_full_url(monkeypatch):
    # Recall reports Teams as a full URL string → normalized the same way ours is.
    _recall_ready(monkeypatch)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse({"results": [_bot(_TEAMS_URL)]}),
    )
    assert main._meeting_has_active_bot(_TEAMS_URL) is True


def test_active_bot_guard_matches_meet_regression(monkeypatch):
    _recall_ready(monkeypatch)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            {"results": [_bot({"meeting_id": "abc-defg-hij"})]}
        ),
    )
    assert main._meeting_has_active_bot(_MEET_URL) is True


def test_active_bot_guard_no_match_for_a_different_meeting(monkeypatch):
    _recall_ready(monkeypatch)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            {"results": [_bot({"meeting_id": "9999999999"})]}
        ),
    )
    assert main._meeting_has_active_bot(_ZOOM_URL) is False


def test_active_bot_guard_ignores_terminal_bots(monkeypatch):
    _recall_ready(monkeypatch)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            {"results": [_bot({"meeting_id": "1234567890"}, code="done")]}
        ),
    )
    assert main._meeting_has_active_bot(_ZOOM_URL) is False


def test_active_bot_guard_no_key_short_circuits_without_network(monkeypatch):
    monkeypatch.setattr(settings, "recall_api_key", "")

    def boom(*a, **k):
        raise AssertionError("must not query Recall without an API key")

    monkeypatch.setattr(main.httpx, "get", boom)
    assert main._meeting_has_active_bot(_ZOOM_URL) is False


def test_reconcile_dedups_two_zoom_bots(monkeypatch):
    # Previously a no-op for Zoom (both bots kept, two meters); now the weaker
    # default variant is told to leave.
    _recall_ready(monkeypatch)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            {"results": [
                _bot({"meeting_id": "1234567890"}, bot_id="old-default", variant=None),
                _bot({"meeting_id": "1234567890"}, bot_id="new-4-core",
                     created_at="2026-07-12T09:01:00Z", variant={"zoom": "web_4_core"}),
            ]}
        ),
    )
    left: list[str] = []
    monkeypatch.setattr(main.recall_client, "leave_call", left.append)
    main._reconcile_duplicate_bots(_ZOOM_URL, "new-4-core")
    assert left == ["old-default"]


def test_reconcile_dedups_two_teams_bots(monkeypatch):
    _recall_ready(monkeypatch)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            {"results": [
                _bot(_TEAMS_URL, bot_id="old-default", variant=None),
                _bot(_TEAMS_URL, bot_id="new-gpu", created_at="2026-07-12T09:01:00Z",
                     variant={"microsoft_teams": "web_gpu"}),
            ]}
        ),
    )
    left: list[str] = []
    monkeypatch.setattr(main.recall_client, "leave_call", left.append)
    main._reconcile_duplicate_bots(_TEAMS_URL, "new-gpu")
    assert left == ["old-default"]


# ── Defect 1: manual /sessions/start durable guard + double-click lock ───────


@pytest.fixture
def start_env(fresh_store, monkeypatch):
    """Stub Recall/ledger/drive for the manual-start path; return the list of
    meeting_urls that create_bot was called with."""
    created: list[str] = []

    def fake_create_bot(meeting_url, avatar_page_url, join_at=None, bot_name="Laura", avatar_id=""):
        created.append(meeting_url)
        return {"id": f"bot_{len(created)}"}

    monkeypatch.setattr(main.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(main.recall_client, "create_bot", fake_create_bot)
    monkeypatch.setattr(main.ledger, "carryover_brief", lambda url, **kw: "")
    monkeypatch.setattr(main.drive_client, "folder_brief", lambda fid: "")
    # Keep the fire-and-forget reconcile out of tests (no background 4s task).
    monkeypatch.setattr(_sessions, "_schedule_start_reconcile", lambda *a, **k: None)
    return created


def test_start_409_when_recall_already_has_bot(start_env, monkeypatch):
    # Post-redeploy: local store empty but Recall still holds the live bot. The
    # durable guard must 409 and NOT dispatch a second bot + meter.
    monkeypatch.setattr(main, "_meeting_has_active_bot", lambda url: True)
    resp = TestClient(main.app).post("/sessions/start", json={"meeting_url": _MEET_URL})
    assert resp.status_code == 409
    assert start_env == []  # create_bot never called


def test_start_then_duplicate_is_409_single_bot(start_env, monkeypatch):
    monkeypatch.setattr(main, "_meeting_has_active_bot", lambda url: False)
    client = TestClient(main.app)
    assert client.post("/sessions/start", json={"meeting_url": _MEET_URL}).status_code == 200
    dup = client.post("/sessions/start", json={"meeting_url": _MEET_URL})
    assert dup.status_code == 409
    assert "bot_id" in dup.json()
    assert start_env == [_MEET_URL]  # exactly one create_bot


def test_start_different_urls_both_dispatch(start_env, monkeypatch):
    monkeypatch.setattr(main, "_meeting_has_active_bot", lambda url: False)
    client = TestClient(main.app)
    assert client.post("/sessions/start", json={"meeting_url": _MEET_URL}).status_code == 200
    assert client.post("/sessions/start", json={"meeting_url": _ZOOM_URL}).status_code == 200
    assert start_env == [_MEET_URL, _ZOOM_URL]  # different meetings still run concurrently


def test_start_lock_keyed_by_meeting_key(fresh_store):
    a = _sessions._start_lock_for(_MEET_URL)
    b = _sessions._start_lock_for(_MEET_URL + "/")  # trailing slash → same meeting_key
    c = _sessions._start_lock_for(_ZOOM_URL)
    assert a is b            # same meeting → shared lock (serialized)
    assert a is not c        # different meeting → distinct lock (parallel)


def test_concurrent_double_start_creates_one_bot(start_env, monkeypatch):
    # Both requests pass the empty-store fast path; the per-meeting lock must
    # serialize the guard→create window so only ONE bot (and one meter) is born.
    import time as _time

    monkeypatch.setattr(main, "_meeting_has_active_bot", lambda url: False)
    calls: list[str] = []

    def slow_create_bot(meeting_url, avatar_page_url, join_at=None, bot_name="Laura", avatar_id=""):
        calls.append(meeting_url)
        _time.sleep(0.1)  # hold the lock so the racing request must wait
        return {"id": f"bot_{len(calls)}"}

    monkeypatch.setattr(main.recall_client, "create_bot", slow_create_bot)

    async def race():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await asyncio.gather(
                c.post("/sessions/start", json={"meeting_url": _MEET_URL}),
                c.post("/sessions/start", json={"meeting_url": _MEET_URL}),
            )

    r1, r2 = asyncio.run(race())
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    assert len(calls) == 1  # lock serialized: exactly one bot dispatched


# ── Defect 3: leave_call 5xx propagates → session kept for reconcile retry ───


def test_leave_call_raises_on_5xx_and_retries(monkeypatch):
    monkeypatch.setattr(recall_client, "_headers", lambda: {"Authorization": "key"})
    captured: dict = {}

    def fake_request(method, url, **kwargs):
        captured["retry"] = kwargs.get("retry")
        return httpx.Response(503, request=httpx.Request(method, url))

    monkeypatch.setattr(recall_client, "_request", fake_request)
    with pytest.raises(httpx.HTTPStatusError):
        recall_client.leave_call("bot_x")
    assert captured["retry"] is True  # mirrors delete_bot's transient-retry window


def test_leave_call_ok_on_200(monkeypatch):
    monkeypatch.setattr(recall_client, "_headers", lambda: {"Authorization": "key"})
    monkeypatch.setattr(
        recall_client, "_request",
        lambda method, url, **k: httpx.Response(200, request=httpx.Request(method, url)),
    )
    assert recall_client.leave_call("bot_x") is None


def _stub_finalize_offline(monkeypatch, delivered: list):
    monkeypatch.setattr(main.anam_client, "end_conversation", lambda c: None)
    monkeypatch.setattr(
        lifecycle, "post_meeting",
        lambda avatar, transcript, **kw: {"summary": "s", "actions": [], "checklist": []},
    )
    monkeypatch.setattr(main.ledger, "record_meeting", lambda *a, **k: None)
    monkeypatch.setattr(
        main.cedric, "deliver_ended",
        lambda integ, bot, art: delivered.append(bot) or True,
    )


def _session_with_transcript(bot_id="bot_x", integration=None, line="hi"):
    s = store.create(bot_id, _MEET_URL, "laura")
    s.memory_brief = ""
    if integration is not None:
        s.integration = integration
    s.add_utterance("Ben", line)
    return s


def test_finalize_keeps_session_on_leave_5xx_but_persists_and_delivers(
    fresh_store, monkeypatch, capsys
):
    delivered: list[str] = []
    _stub_finalize_offline(monkeypatch, delivered)
    monkeypatch.setattr(
        main.recall_client, "leave_call",
        lambda bot_id: (_ for _ in ()).throw(_http_status_error(503)),
    )
    secret_line = "ship the DPA to Acme by Friday"
    _session_with_transcript(integration={"callback_url": "https://cb/events"}, line=secret_line)

    artifact = asyncio.run(main._finalize_session("bot_x", source="webhook"))

    kept = store.get("bot_x")
    assert kept is not None                       # session KEPT (reconcile can retry)
    assert getattr(kept, "leave_pending") is True
    assert store.get_artifact("bot_x") is not None  # deliverable still persisted
    assert delivered == ["bot_x"]                  # delivered once, not lost
    assert artifact["summary"] == "s"
    assert secret_line not in capsys.readouterr().out  # no transcript logged (PII)


def test_finalize_normal_leave_removes_and_delivers(fresh_store, monkeypatch):
    delivered: list[str] = []
    _stub_finalize_offline(monkeypatch, delivered)
    monkeypatch.setattr(main.recall_client, "leave_call", lambda bot_id: None)  # 200 OK
    _session_with_transcript(integration={"callback_url": "https://cb/events"})

    artifact = asyncio.run(main._finalize_session("bot_x", source="manual"))

    assert store.get("bot_x") is None              # removed as before
    assert delivered == ["bot_x"]
    assert artifact["summary"] == "s"


def test_finalize_404_gone_leave_still_removes(fresh_store, monkeypatch):
    # A 404 "bot already gone" (natural end) is a CONFIRMED not-billing state; it
    # must finalize normally, not get stranded in leave_pending forever.
    delivered: list[str] = []
    _stub_finalize_offline(monkeypatch, delivered)
    monkeypatch.setattr(
        main.recall_client, "leave_call",
        lambda bot_id: (_ for _ in ()).throw(_http_status_error(404)),
    )
    _session_with_transcript()

    asyncio.run(main._finalize_session("bot_x", source="reconcile"))
    assert store.get("bot_x") is None


@pytest.mark.parametrize("status", [401, 403, 429])
def test_finalize_keeps_session_on_unverified_leave(fresh_store, monkeypatch, status):
    # BLOCKER 1: an auth/rate-limit failure (a rotated Recall key returns 401 on
    # every leave) must be treated as UNVERIFIED; keep the session so reconcile
    # retries, NOT silently "confirmed stopped" (which removed it → fleet leak).
    delivered: list[str] = []
    _stub_finalize_offline(monkeypatch, delivered)
    monkeypatch.setattr(
        main.recall_client, "leave_call",
        lambda bot_id: (_ for _ in ()).throw(_http_status_error(status)),
    )
    _session_with_transcript(integration={"callback_url": "https://cb/events"})

    asyncio.run(main._finalize_session("bot_x", source="webhook"))

    kept = store.get("bot_x")
    assert kept is not None                       # NOT removed
    assert getattr(kept, "leave_pending") is True
    assert delivered == ["bot_x"]                  # still delivered once (not lost)


def test_leave_confirmed_stopped_classification():
    assert main._leave_confirmed_stopped(None) is True          # success
    assert main._leave_confirmed_stopped(_http_status_error(404)) is True   # gone
    assert main._leave_confirmed_stopped(_http_status_error(410)) is True   # gone
    for bad in (401, 403, 429, 500, 502, 503):
        assert main._leave_confirmed_stopped(_http_status_error(bad)) is False
    assert main._leave_confirmed_stopped(httpx.ConnectError("boom")) is False


def test_retry_leave_keeps_on_persistent_5xx(fresh_store, monkeypatch):
    s = _session_with_transcript()
    s.leave_pending = True
    monkeypatch.setattr(
        main.recall_client, "leave_call",
        lambda bot_id: (_ for _ in ()).throw(_http_status_error(503)),
    )
    # Status poll (the terminal-drain check) sees a STILL-LIVE bot → keep it.
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse({"status_changes": [{"code": "in_call_recording"}]}),
    )
    assert asyncio.run(main._retry_leave("bot_x", s)) is False
    assert store.get("bot_x") is not None
    store.remove("bot_x")


def test_retry_leave_removes_on_gone_404(fresh_store, monkeypatch):
    # BLOCKER 3: a 404 on retry (bot already ended) must DROP the session, not
    # retry it forever (which would inflate active_sessions / the pre-deploy gate).
    s = _session_with_transcript()
    s.leave_pending = True
    monkeypatch.setattr(
        main.recall_client, "leave_call",
        lambda bot_id: (_ for _ in ()).throw(_http_status_error(404)),
    )
    assert asyncio.run(main._retry_leave("bot_x", s)) is True
    assert store.get("bot_x") is None


def test_retry_leave_removes_on_success_and_signals_meter_once(fresh_store, monkeypatch):
    s = _session_with_transcript()
    s.leave_pending = True
    monkeypatch.setattr(main.recall_client, "leave_call", lambda bot_id: None)
    signals: list[int] = []
    monkeypatch.setattr(main.gpu_runtime, "on_session_ended", lambda n: signals.append(n))
    monkeypatch.setattr(main.runpod_runtime, "on_session_ended", lambda n: signals.append(n))

    assert asyncio.run(main._retry_leave("bot_x", s)) is True
    assert store.get("bot_x") is None
    assert len(signals) == 2   # gpu + runpod, once each; no rebuild/re-deliver loop


def test_retry_leave_defers_to_in_flight_finalize(fresh_store, monkeypatch):
    # Defense-in-depth: if a finalize already owns the bot, retry must no-op
    # (not race a second concurrent leave/remove).
    s = _session_with_transcript()
    s.leave_pending = True
    main._finalizing.add("bot_x")
    left: list[str] = []
    monkeypatch.setattr(main.recall_client, "leave_call", left.append)
    try:
        assert asyncio.run(main._retry_leave("bot_x", s)) is False
    finally:
        main._finalizing.discard("bot_x")
    assert left == []                       # never touched leave_call
    assert store.get("bot_x") is not None    # session left intact
    store.remove("bot_x")


def test_store_reload_preserves_leave_pending(fresh_store):
    # BLOCKER 2: a deploy restart re-hydrates sessions from sqlite. leave_pending
    # must survive, else the session reverts to "in progress" and reconcile polls
    # status instead of retrying the leave; the leak re-strands.
    s = store.create("bot_persist", _MEET_URL, "laura")
    s.leave_pending = True                   # persisted via __setattr__
    store._load_from_db()                    # simulate a process restart
    reloaded = store.get("bot_persist")
    assert reloaded is not None
    assert reloaded.leave_pending is True
    store.remove("bot_persist")


def _no_poll(*a, **k):
    raise AssertionError("leave_pending must retry leave directly, never poll status")


def test_reconcile_retries_leave_pending_and_removes_on_success(fresh_store, monkeypatch):
    s = _session_with_transcript()
    store.save_artifact("bot_x", {"summary": "s"})  # already delivered earlier
    s.leave_pending = True
    left: list[str] = []
    monkeypatch.setattr(main.recall_client, "leave_call", left.append)
    monkeypatch.setattr(main.httpx, "get", _no_poll)  # must not poll the (still-live) bot
    delivered: list[int] = []
    monkeypatch.setattr(main.cedric, "deliver_ended", lambda *a, **k: delivered.append(1) or True)

    asyncio.run(main._reconcile_once())

    assert left == ["bot_x"]           # meter-stop retried
    assert store.get("bot_x") is None   # dropped after a confirmed leave
    assert delivered == []              # artifact NOT re-delivered


def test_reconcile_keeps_leave_pending_when_retry_still_fails(fresh_store, monkeypatch):
    s = _session_with_transcript()
    store.save_artifact("bot_x", {"summary": "s"})
    s.leave_pending = True
    monkeypatch.setattr(
        main.recall_client, "leave_call",
        lambda bot_id: (_ for _ in ()).throw(_http_status_error(503)),
    )
    monkeypatch.setattr(main.httpx, "get", _no_poll)

    asyncio.run(main._reconcile_once())

    assert store.get("bot_x") is not None  # still kept; retry again next pass
    store.remove("bot_x")
