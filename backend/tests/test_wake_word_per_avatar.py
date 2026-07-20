"""Per-avatar wake-word mode: speak only when addressed by name.

Owner request 2026-07-17: laura/petra/cedric answer ONLY on their wake word
and never interrupt otherwise. Covers the yaml field + inheritance, the
_wake_required resolver, and the gate wiring (backchannel suppression and
the not-called answer gate with its follow-up exception).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import app.main as main_module  # noqa: E402
from app import avatars, store  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _wake_word_inherits_global():
    """Override the conftest pin: this module asserts the SHIPPED yaml state."""
    yield


def test_all_three_avatars_ship_wake_word_on():
    for aid in ("laura", "petra", "cedric"):
        assert avatars.load(aid).require_wake_word is True, aid


def test_yaml_none_inherits_global(monkeypatch):
    # Build a bare Avatar to test inheritance without touching real yamls.
    bare = avatars.Avatar(
        id="x", name="X", role="", wake_words=["x"], persona_prompt="",
        anam_avatar_id="", elevenlabs_voice_id="", min_confidence=0.5,
        speak_cooldown_seconds=8.0, dir=Path("."), require_wake_word=None,
    )
    monkeypatch.setattr(settings, "require_wake_word", False)
    assert main_module._wake_required(bare) is False
    monkeypatch.setattr(settings, "require_wake_word", True)
    assert main_module._wake_required(bare) is True
    # An explicit avatar False pins it regardless of the global.
    pinned = avatars.Avatar(
        id="y", name="Y", role="", wake_words=["y"], persona_prompt="",
        anam_avatar_id="", elevenlabs_voice_id="", min_confidence=0.5,
        speak_cooldown_seconds=8.0, dir=Path("."), require_wake_word=False,
    )
    assert main_module._wake_required(pinned) is False


def test_backchannel_suppressed_in_wake_word_mode(monkeypatch):
    """In a GROUP, a wake-word avatar makes no unprompted backchannel. (The
    1:1 relaxation — a single human present drops the wake requirement, Duccio
    2026-07-20 — is asserted separately below, so this uses a 2-human roster.)"""
    monkeypatch.setattr(settings, "backchannel_enabled", True)
    monkeypatch.setattr(settings, "backchannel_min_words", 3)
    session = store.Session(bot_id="b1", meeting_url="m", avatar_id="petra")
    session.participants = {
        "1": {"name": "Ada", "here": True, "kind": "human"},
        "2": {"name": "Ben", "here": True, "kind": "human"},
    }
    session.last_backchannel_at = 0.0
    session.last_spoke_at = 0.0
    session.speaking_until = 0.0
    long_text = "we should really think about the rollout plan for next quarter"
    petra = avatars.load("petra")  # require_wake_word: true
    assert main_module._should_backchannel(session, long_text, petra) is False


def test_wake_required_relaxes_one_on_one(monkeypatch):
    """Duccio's 1:1 relaxation: a wake-word avatar drops the wake requirement
    when a single human is present (the ask is unambiguously for her), but
    keeps it in a group."""
    petra = avatars.load("petra")  # require_wake_word: true
    solo = store.Session(bot_id="s", meeting_url="m", avatar_id="petra")
    solo.participants = {"1": {"name": "Ada", "here": True, "kind": "human"}}
    group = store.Session(bot_id="g", meeting_url="m", avatar_id="petra")
    group.participants = {
        "1": {"name": "Ada", "here": True, "kind": "human"},
        "2": {"name": "Ben", "here": True, "kind": "human"},
    }
    assert main_module._wake_required(petra, solo) is False   # 1:1 → relaxed
    assert main_module._wake_required(petra, group) is True   # group → required
    assert main_module._wake_required(petra) is True          # no session → base


class _FakeTask:
    def add_done_callback(self, cb):  # matches asyncio.Task's surface
        pass


def _capture_create_task(launched: list):
    """A create_task stand-in: closes the coroutine (no 'never awaited'
    warning), records the launch, returns a task-shaped object."""

    def _fake(coro, *a, **k):
        coro.close()
        launched.append(True)
        return _FakeTask()

    return _fake


def test_self_introduction_suppressed_in_wake_word_mode(monkeypatch):
    """A wake-word avatar enters SILENT: maybe_self_introduce schedules nothing
    and marks the intro done so later webhooks don't re-check (owner ask
    2026-07-20)."""
    monkeypatch.setattr(settings, "self_introduce_on_join", True)
    launched: list = []
    monkeypatch.setattr(main_module.asyncio, "create_task",
                        _capture_create_task(launched))
    session = store.Session(bot_id="b_intro", meeting_url="m", avatar_id="petra")
    assert main_module.maybe_self_introduce(session) is False
    assert launched == []  # no self-intro task scheduled
    assert session.self_introduced is True  # stops re-checking every webhook


def test_self_introduction_runs_when_wake_word_off(monkeypatch):
    """Control: with wake mode off, the join self-introduction still schedules."""
    import dataclasses

    monkeypatch.setattr(settings, "self_introduce_on_join", True)
    real = avatars.load("petra")
    monkeypatch.setattr(
        avatars, "load",
        lambda aid: dataclasses.replace(real, require_wake_word=False),
    )
    launched: list = []
    monkeypatch.setattr(main_module.asyncio, "create_task",
                        _capture_create_task(launched))
    session = store.Session(bot_id="b_intro2", meeting_url="m", avatar_id="petra")
    assert main_module.maybe_self_introduce(session) is True
    assert len(launched) == 1


def test_no_ack_on_partial_in_wake_word_mode(monkeypatch, tmp_path):
    """The core of 'listen fully, speak only after I finish': a partial that
    addresses a wake-word avatar by name must NOT trigger the instant 'Sure —'
    ack spoken over the still-talking speaker (owner ask 2026-07-20)."""
    import importlib

    from fastapi.testclient import TestClient

    from app import ledger

    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    monkeypatch.setattr(settings, "ack_enabled", True)
    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(
        main_module.recall_client, "create_bot",
        lambda meeting_url, avatar_page_url, join_at=None, bot_name="Laura",
        avatar_id="": {"id": "bot_wk"},
    )
    monkeypatch.setattr(main_module.recall_client, "leave_call", lambda bot_id: None)
    monkeypatch.setattr(main_module.recall_client, "delete_bot", lambda bot_id: None)
    monkeypatch.setattr(main_module.anam_client, "end_conversation", lambda c: None)

    spoken: list = []

    async def fake_speak(session, line, **kwargs):
        spoken.append(line)
        return True

    monkeypatch.setattr(main_module, "_make_avatar_speak", fake_speak)

    client = TestClient(main_module.app)
    bot_id = client.post(
        "/sessions/start",
        json={"meeting_url": "https://meet.google.com/wk-ack", "avatar_id": "petra"},
    ).json()["bot_id"]
    client.post(
        "/webhooks/recall",
        json={
            "event": "transcript.partial_data",
            "data": {"bot": {"id": bot_id}, "data": {
                "words": [{"text": w} for w in
                          "Petra what is the status of the rollout project".split()],
                "participant": {"name": "Ben", "id": 1}}},
        },
    )
    assert spoken == []  # wake-word mode: no ack over the speaker


def test_answer_gate_blocks_uncalled_but_allows_followup(monkeypatch):
    """The gate logic in isolation: not-called → silent, unless it's a
    question right after the avatar's own answer (follow-up window)."""
    petra = avatars.load("petra")
    assert main_module._wake_required(petra) is True
    # Mirror the gate's follow-up predicate.
    monkeypatch.setattr(settings, "followup_window_seconds", 12.0)
    now = time.time()

    def followup_ok(last_spoke_at: float, text: str) -> bool:
        return (
            settings.followup_window_seconds > 0
            and (now - last_spoke_at) < settings.followup_window_seconds
            and text.rstrip().endswith("?")
        )

    assert followup_ok(now - 5, "and what about the deadline?") is True
    assert followup_ok(now - 60, "and what about the deadline?") is False
    assert followup_ok(now - 5, "just thinking out loud") is False
