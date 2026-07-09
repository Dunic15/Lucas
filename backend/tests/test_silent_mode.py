"""Silent-mode MECHANISM: an avatar with silent=True never speaks during the
meeting, but still captures the transcript / builds the artifact. Cedric and
Laura both ship TALKING (silent=False) — Cedric interacts like a colleague and
captures tasks in the background. Key-free."""
from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars, main, store


def _session(avatar_id: str, bot_id: str = "silent-bot") -> store.Session:
    s = store.Session(bot_id=bot_id, meeting_url="https://meet.example/s", avatar_id=avatar_id)
    object.__setattr__(s, "_persist_enabled", False)
    return s


def test_cedric_and_laura_both_talk():
    # Both ship talking: Cedric interacts in the meeting AND captures tasks.
    assert avatars.load("cedric").silent is False
    assert avatars.load("laura").silent is False


def test_silent_flag_suppresses_speech(monkeypatch):
    # The MECHANISM: an avatar configured silent never speaks. Since no shipped
    # avatar is silent now, drive it with a synthetic silent avatar.
    real = avatars.load("laura")
    silent_av = replace(real, silent=True) if hasattr(real, "__dataclass_fields__") else real
    monkeypatch.setattr(main.avatars, "load", lambda aid: silent_av)
    sent = []
    s = _session("whoever")

    class FakeWS:
        async def send_json(self, m):
            sent.append(m)

    s.ws = FakeWS()
    # force=True would normally bypass the repeat guard and speak — silent wins.
    spoke = asyncio.run(main._make_avatar_speak(s, "Hello everyone.", force=True))
    assert spoke is False
    assert sent == []  # nothing was pushed to the page


def test_talking_avatar_still_speaks():
    sent = []
    s = _session("laura", bot_id="talk-bot")

    class FakeWS:
        async def send_json(self, m):
            sent.append(m)

    s.ws = FakeWS()
    spoke = asyncio.run(main._make_avatar_speak(s, "Hello from Laura.", force=True))
    assert spoke is True
    assert sent and sent[0]["type"] == "speak"


def test_silent_session_still_captures(tmp_path, monkeypatch):
    # Capture is independent of speaking (the silent gate is only in
    # _make_avatar_speak), so a silent meeting still records the transcript that
    # the finalize artifact is built from.
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    s = store.create(bot_id="silent-artifact", meeting_url="https://meet.example/s",
                     avatar_id="cedric")
    s.add_utterance("Duccio", "We decided to launch on Monday.")
    s.add_utterance("Marco", "I will own the rollout doc.")
    assert len(s.transcript) == 2  # transcript captured despite silence
    assert s.transcript_text()  # non-empty → artifact can be built at finalize
