"""Robustness-audit regressions (6 adversarially-confirmed defects). Key-free:
no vendors, no network, no secrets — brain/Recall/callbacks are all stubbed.

  1. A mid-stream LLM drop AFTER the first spoken sentence must NOT 500 the live
     webhook (Recall would re-deliver + cut her off): she says ONE recovery line
     and the route returns 200. A PRE-token drop keeps today's behavior.
  2. store.roster() must not strip a HUMAN named "Laura" from a non-Laura
     meeting — only the running avatar's own name.
  3. A transient post-meeting model failure at finalize must DEGRADE to a
     deterministic recap (saved + delivered), never 500 /end and lose it.
  4. /redeliver must be fire-and-forget (202) — never block ~150s on send_ended's
     retry chain and 504.
  5. Manual /deliver must stamp the follow-up with the artifact's avatar name
     (Cedric), not the default Laura, after finalize removed the session.
  6. /demo/post_meeting + /demo/sample must 404 (not 500) on an unknown avatar.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import main, ledger, store  # noqa: E402
from app.config import settings  # noqa: E402

_MEET_URL = "https://meet.google.com/abc-defg-hij"


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Clean, isolated sqlite store + no leftover in-flight state."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    main._finalizing.clear()
    main._reconcile_missing.clear()
    yield
    main._finalizing.clear()
    main._reconcile_missing.clear()


# ── Fix 1: a mid-stream LLM drop must not 500 the live webhook ───────────────


def _live_session(tmp_path, monkeypatch, bot_id="recover-bot"):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, _MEET_URL, "laura")
    s.memory_brief = ""            # non-None → no ledger call
    s.addressed_once = True        # already activated (opening grace passed)
    s.participant_event("Ben", 1, here=True)  # a single human → direct answer
    monkeypatch.setattr(settings, "deference_seconds", 0)
    monkeypatch.setattr(settings, "recall_api_key", "")
    return s


def _fake_request(payload: dict):
    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    return FakeRequest()


def _line(bot_id: str, speaker: str, text: str) -> dict:
    return {
        "event": "transcript.data",
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "words": [{"text": w} for w in text.split()],
                "participant": {"name": speaker, "id": 1},
            },
        },
    }


def _capture_speech(monkeypatch):
    """Capture the loop's speak path (_speak_with_audio), honouring `prev` so the
    spoken ORDER is deterministic (sentence(s) first, recovery last)."""
    spoken: list[str] = []

    async def fake_speak(session, text, *, force, generation, prev=None, t0=None):
        if prev is not None:
            try:
                await prev
            except Exception:  # noqa: BLE001
                pass
        spoken.append(text)
        return True

    monkeypatch.setattr(main, "_speak_with_audio", fake_speak)
    return spoken


def _stream_raise_after(monkeypatch, sentences, top_score=0.9):
    exc = RuntimeError("cerebras 529 overloaded")

    def stream(*a, **k):
        if k.get("meta") is not None:
            k["meta"]["top_score"] = top_score
        for s in sentences:
            yield s
        raise exc  # provider blip AFTER the first token (llm.py re-raises it)

    monkeypatch.setattr(main, "answer_question_stream", stream)


def _stream_raise_before(monkeypatch):
    exc = RuntimeError("cerebras 529 overloaded")

    def stream(*a, **k):
        if k.get("meta") is not None:
            k["meta"]["top_score"] = 0.0
        raise exc
        yield  # unreachable; keeps generator semantics so the raise fires on iter

    monkeypatch.setattr(main, "answer_question_stream", stream)


def test_midstream_drop_speaks_one_recovery_line_and_returns_200(
    tmp_path, monkeypatch, capsys
):
    s = _live_session(tmp_path, monkeypatch)
    _stream_raise_after(monkeypatch, ["Managers approve access first."])
    spoken = _capture_speech(monkeypatch)

    secret = "what is the exact provisioning approval chain?"
    resp = asyncio.run(main.recall_webhook(_fake_request(_line(s.bot_id, "Ben", f"Laura, {secret}"))))

    # 200 (not 500) so Recall does NOT re-deliver the turn.
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body.get("recovered") is True and body.get("spoke") is True
    # She said the first sentence, then exactly ONE recovery line, same path.
    assert spoken[0] == "Managers approve access first."
    assert spoken[-1] in main._STREAM_RECOVERY_LINES
    assert len(spoken) == 2
    # No transcript logged (PII): the question text never reaches stdout.
    assert secret not in capsys.readouterr().out
    store.remove(s.bot_id)


def test_pretoken_drop_preserves_existing_behavior_no_silent_swallow(
    tmp_path, monkeypatch
):
    s = _live_session(tmp_path, monkeypatch, bot_id="recover-bot-pre")
    _stream_raise_before(monkeypatch)
    spoken = _capture_speech(monkeypatch)

    # Nothing spoken yet → the failure is NOT swallowed into a recovery line; it
    # propagates (today's behavior: 500 → Recall re-delivers, no dup risk, and
    # llm.py's own pre-token Haiku fallback covers the real-key case).
    with pytest.raises(RuntimeError):
        asyncio.run(
            main.recall_webhook(_fake_request(_line(s.bot_id, "Ben", "Laura, what's the plan?")))
        )
    assert spoken == []
    store.remove(s.bot_id)


# ── Fix 2: roster() keeps a human named "Laura" in a non-Laura meeting ───────


def _detached_session(bot_id):
    s = store.Session(bot_id=bot_id, meeting_url="https://meet.test")
    object.__setattr__(s, "_persist_enabled", False)
    return s


def test_roster_keeps_human_named_laura_in_cedric_meeting():
    s = _detached_session("cedric-bot")
    s.participant_event("Laura", 1, here=True)  # a HUMAN named Laura
    s.participant_event("Ben", 2, here=True)
    roster = s.roster("cedric")  # the running avatar is Cedric
    assert "Laura" in roster
    assert {n.lower() for n in roster} == {"laura", "ben"}


def test_roster_still_strips_avatars_own_name_when_avatar_is_laura():
    s = _detached_session("laura-bot")
    s.participant_event("Laura", 1, here=True)  # the avatar's OWN name
    s.participant_event("Ben", 2, here=True)
    roster = s.roster("Laura")
    assert roster == ["Ben"]  # the avatar itself is not a human in the room


def test_present_names_forwards_avatar_name_to_roster():
    # present_names()'s "everyone besides the avatar itself" contract must stay
    # literally true after Fix 2: forwarding the avatar name keeps its OWN name
    # out of the fuzzy-wake exclude set (so a corruption of its wake word still
    # wakes it), while a legacy no-arg call is unchanged.
    s = _detached_session("laura-bot-pn")
    s.participant_event("Laura", 1, here=True)  # avatar's own name as a participant
    s.participant_event("Ben", 2, here=True)
    named = {n.lower() for n in s.present_names("Laura")}
    assert "laura" not in named and "ben" in named
    # No-arg (legacy) still returns everyone, a Laura participant included.
    assert "laura" in {n.lower() for n in s.present_names()}


# ── Fix 3: a transient post-meeting failure degrades, never loses the artifact ─


def test_finalize_degrades_when_post_meeting_raises(fresh_store, monkeypatch, capsys):
    left: list[str] = []
    delivered: list[tuple] = []
    monkeypatch.setattr(main.anam_client, "end_conversation", lambda c: None)
    monkeypatch.setattr(main.ledger, "record_meeting", lambda *a, **k: None)
    monkeypatch.setattr(main.recall_client, "leave_call", left.append)  # 200 OK meter stop
    monkeypatch.setattr(
        main.cedric, "deliver_ended",
        lambda integ, bot, art: delivered.append((bot, art)) or True,
    )

    def boom(*a, **k):
        raise RuntimeError("cerebras 529 overloaded")  # transient post-model failure

    monkeypatch.setattr(main, "post_meeting", boom)

    secret = "ship the SOC2 report to Globex by Tuesday"
    s = store.create("bot_x", _MEET_URL, "laura")
    s.memory_brief = ""
    s.integration = {"callback_url": "https://cb/events"}
    s.add_utterance("Ben", secret)

    artifact = asyncio.run(main._finalize_session("bot_x", source="manual"))

    # Degraded (not lost): a non-empty deterministic recap is built …
    assert artifact is not None and artifact.get("summary")
    assert "tracker" in artifact["summary"].lower()  # the degraded recap wording
    assert store.get_artifact("bot_x") is not None    # … saved …
    assert delivered and delivered[0][0] == "bot_x"   # … and delivered.
    # The meter-stop / leave path still ran and the session was removed on 200.
    assert left == ["bot_x"]
    assert store.get("bot_x") is None
    # No transcript logged (PII).
    assert secret not in capsys.readouterr().out


def test_finalize_saves_bare_scaffold_when_degraded_recap_also_raises(
    fresh_store, monkeypatch, capsys
):
    # Hardening on Fix 3: if the degraded rebuild ALSO throws (a real bug in
    # build_from_text/_finish_artifact, not an LLM blip), finalize must STILL not
    # 500 or lose the meeting — it saves + delivers a bare deterministic scaffold.
    left: list[str] = []
    delivered: list[tuple] = []
    monkeypatch.setattr(main.anam_client, "end_conversation", lambda c: None)
    monkeypatch.setattr(main.ledger, "record_meeting", lambda *a, **k: None)
    monkeypatch.setattr(main.recall_client, "leave_call", left.append)  # 200 OK meter stop
    monkeypatch.setattr(
        main.cedric, "deliver_ended",
        lambda integ, bot, art: delivered.append((bot, art)) or True,
    )

    def boom(*a, **k):
        raise RuntimeError("cerebras 529 overloaded")  # transient LLM failure

    def boom_bug(*a, **k):
        raise ValueError("build_from_text regression")  # a REAL code bug

    monkeypatch.setattr(main, "post_meeting", boom)
    monkeypatch.setattr(main, "degraded_post_meeting", boom_bug)

    secret = "rotate the prod DB password before Thursday"
    s = store.create("bot_bare", _MEET_URL, "laura")
    s.memory_brief = ""
    s.integration = {"callback_url": "https://cb/events"}
    s.add_utterance("Ben", secret)

    artifact = asyncio.run(main._finalize_session("bot_bare", source="manual"))

    # /end still returns a (bare) artifact — never 500, never lost.
    assert artifact is not None
    assert "recap unavailable" in artifact["summary"].lower()
    assert store.get_artifact("bot_bare") is not None
    assert delivered and delivered[0][0] == "bot_bare"
    # Meter / leave path still ran; session removed on a 200 leave.
    assert left == ["bot_bare"]
    assert store.get("bot_bare") is None
    out = capsys.readouterr().out
    # The bug SURFACES (stack logged) …
    assert "bare scaffold" in out and "ValueError" in out
    # … but no transcript is logged (PII), even alongside the traceback.
    assert secret not in out


# ── Fix 4: /redeliver is fire-and-forget (202), never blocks on retries ──────


def test_redeliver_returns_202_without_blocking_on_send_retries(fresh_store, monkeypatch):
    store.save_artifact("bot_fast", {"summary": "s", "org_id": "", "actions": []})
    monkeypatch.setattr(settings, "surface_webhook_url", "https://cb/events")

    def slow_send(integration, bot_id, wire):
        time.sleep(0.6)  # stand in for send_ended's ~150s blocking retry chain
        return True

    monkeypatch.setattr(main.cedric.callback, "send_ended", slow_send)

    client = TestClient(main.app)
    t0 = time.perf_counter()
    resp = client.post("/sessions/bot_fast/redeliver")
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 202
    assert resp.json().get("status") == "retrying"
    # Awaiting slow_send would take >= 0.6s; scheduling it does not.
    assert elapsed < 0.4


def test_deliver_ended_commits_outbox_before_return(monkeypatch):
    # Durable delivery replaces fragile in-memory task ownership: the callback
    # envelope must be committed first, then delivery may be nudged off-path.
    from app.cedric import integration as ci

    calls: list[tuple] = []
    kicks: list[bool] = []
    monkeypatch.setattr(
        ci.outbox,
        "enqueue_session_ended",
        lambda integration, bot_id, artifact: calls.append(
            (integration, bot_id, artifact)
        ) or "outbox-1",
    )
    monkeypatch.setattr(ci, "_kick_outbox", lambda: kicks.append(True))

    ok = ci.deliver_ended(
        {"callback_url": "https://cb"}, "bot_ref", {"summary": "s"}
    )

    assert ok is True
    assert len(calls) == 1
    assert calls[0][1] == "bot_ref"
    assert calls[0][2]["summary"] == "s"
    assert kicks == [True]


# ── Fix 5: manual /deliver stamps the correct avatar name after finalize ─────


def test_deliver_stamps_cedric_name_from_artifact_after_finalize(fresh_store, monkeypatch):
    # Finalize already ran store.remove(bot_id): the live session is gone, so the
    # name MUST come from the saved artifact's avatar_id, not the default Laura.
    store.save_artifact(
        "bot_c",
        {
            "summary": "s",
            "avatar_id": "cedric",
            "org_id": "",
            "follow_up_email": {"subject": "x", "body": "y"},
            "actions": [],
        },
    )
    assert store.get("bot_c") is None  # no live session (post-finalize state)

    captured: dict = {}

    def fake_slack(name, artifact):
        captured["name"] = name
        return "slack text"

    monkeypatch.setattr(main.actions, "send_email", lambda to, subj, body: {"sent": False})
    monkeypatch.setattr(main.actions, "artifact_to_slack_text", fake_slack)
    monkeypatch.setattr(main.actions, "post_to_slack", lambda text: {"sent": True})

    client = TestClient(main.app)
    resp = client.post("/sessions/bot_c/deliver", json={"to": [], "slack": True})

    assert resp.status_code == 200
    assert captured["name"] == "Cedric"  # was falling back to 'Laura'


# ── Fix 6: demo routes 404 (not 500) on an unknown avatar_id ─────────────────


def test_demo_post_meeting_unknown_avatar_returns_404():
    client = TestClient(main.app)
    resp = client.post(
        "/demo/post_meeting", json={"transcript": "Ben: hi", "avatar_id": "no-such-avatar"}
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "unknown avatar_id"
    assert "laura" in body["available"]


def test_demo_sample_unknown_avatar_returns_404():
    client = TestClient(main.app)
    resp = client.get("/demo/sample", params={"avatar_id": "no-such-avatar"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "unknown avatar_id"
    assert isinstance(body["available"], list) and "laura" in body["available"]
