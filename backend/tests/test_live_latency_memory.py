"""Live-path latency instrumentation + conversational memory (2026-07-23).

Two live regressions this covers:

  LATENCY — #421 added an unclipped `own_recent` block (her last 3 full answers)
  to every prompt; input tokens are first-token latency on the live path. This
  asserts the per-stage telemetry exists, is durations/sizes/flags ONLY (never
  transcript text), and that the new prompt is not larger than #421's.

  MEMORY — she recalled only the last ~2 questions and, on a short follow-up
  fragment ("and before?"), replayed her previous multi-sentence answer
  verbatim. This asserts the questions-recap reaches the model and that a short
  follow-up cannot re-speak her last turn near-verbatim.

Key-free: the brain is forced onto a fake `llm` seam and retrieval is stubbed.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.actions import ledger  # noqa: E402
from app.brain import engine as brain  # noqa: E402
from app.avatars import Avatar  # noqa: E402
from app.rag import Retrieved  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────────
def _avatar(**overrides) -> Avatar:
    base = dict(
        id="laura", name="Laura", role="AI Process Expert", wake_words=["laura"],
        persona_prompt="", anam_avatar_id="r1", elevenlabs_voice_id="v1",
        min_confidence=0.55, speak_cooldown_seconds=8.0, dir=Path("."),
    )
    base.update(overrides)
    return Avatar(**base)


def _mem_session(bot_id="latmem-bot") -> store.Session:
    s = store.Session(bot_id=bot_id, meeting_url="m", org_id="demo")
    object.__setattr__(s, "_persist_enabled", False)  # in-memory only, no FK
    return s


def _force_streaming(monkeypatch, captured: dict, sentences=None) -> None:
    monkeypatch.setattr(brain.settings, "brain_provider", "anthropic")
    monkeypatch.setattr(brain.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_live_route", lambda q: ("anthropic", "m"))

    def fake_stream(system, user, *a, **k):
        captured["system"] = system
        captured["user"] = user
        return iter(sentences or ["Sure, here is the answer."])

    monkeypatch.setattr(brain.llm, "stream_complete", fake_stream)


# ── MEMORY: questions-only recall reaches past the recent-line window ─────────
def test_recent_user_questions_recalls_more_than_two():
    s = _mem_session()
    turns = [
        "Which tools can you use?",
        "we should ship friday",           # statement — not a question
        "Are you connected to Asana?",
        "What actions can you take in Gmail?",
        "Who owns the migration?",
        "and before that?",
    ]
    for i, t in enumerate(turns):
        s.add_utterance(speaker="Duccio", text=t, speaker_kind="human")
        s.add_utterance(speaker="Laura", text="ok", speaker_kind="agent")
    qs = s.recent_user_questions(6)
    # The live bug was "remembered only the last 2"; the questions view recalls
    # every interrogative in the window, and drops the plain statement.
    assert len(qs) > 2
    assert "we should ship friday" not in qs
    assert "Which tools can you use?" in qs


def test_questions_recap_is_injected_into_the_live_prompt(monkeypatch):
    captured: dict = {}
    _force_streaming(monkeypatch, captured)
    out = list(
        brain.answer_question_stream(
            _avatar(), "and before that?",
            questions="Which tools can you use?\nAre you connected to Asana?\nWho owns the migration?",
            speaker="Duccio",
        )
    )
    assert out
    # The model is handed the earlier questions so "what did I ask before?" is
    # answerable — and told what the block is for.
    assert "Which tools can you use?" in captured["user"]
    assert "Who owns the migration?" in captured["user"]
    assert "what did i ask" in captured["user"].lower()


def test_no_questions_leaves_the_prompt_block_absent(monkeypatch):
    captured: dict = {}
    _force_streaming(monkeypatch, captured)
    list(brain.answer_question_stream(_avatar(), "What is the access flow?"))
    assert "asked you earlier" not in captured["user"]


# ── LATENCY: per-stage telemetry exists and carries NO transcript text ────────
def test_engine_meta_exposes_pii_free_stage_telemetry(monkeypatch):
    monkeypatch.setattr(brain.settings, "brain_provider", "stub")
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: [])
    secret_q = "what is our confidential acquisition target"
    secret_hist = "Duccio: the target is Northwind Corp"
    meta: dict = {}
    list(
        brain.answer_question_stream(
            _avatar(), secret_q,
            history=secret_hist, own_recent="You said: earlier thing",
            questions="prior question?", meta=meta,
        )
    )
    # Every stage/flag/size the [latency] line needs is present…
    for key in ("rag_skipped", "retrieve_ms", "first_token_ms", "web_search",
                "model", "own_chars", "hist_chars", "questions_chars"):
        assert key in meta, f"missing telemetry key: {key}"
    # …and every value is a number / bool / model-name — never transcript text.
    assert isinstance(meta["retrieve_ms"], (int, float))
    assert isinstance(meta["first_token_ms"], (int, float))
    assert isinstance(meta["web_search"], bool)
    assert meta["own_chars"] == len("You said: earlier thing")
    assert meta["hist_chars"] == len(secret_hist)
    blob = " ".join(str(v) for v in meta.values())
    assert "Northwind" not in blob
    assert "acquisition" not in blob


# ── the answer-path webhook harness (mirrors test_live_followup_and_corrections)
def _session(tmp_path, monkeypatch, bot_id="latmem-web") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    ledger._init_db()  # the answer path builds a carryover brief from ledger_items
    s = store.create(bot_id, "https://meet.google.com/lat-mem-tst", "laura")
    s.addressed_once = True
    return s


def _line(bot_id: str, speaker: str, pid, text: str) -> dict:
    return {
        "event": "transcript.data",
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "words": [{"text": w} for w in text.split()],
                "participant": {"name": speaker, "id": pid},
            },
        },
    }


def _post(payload: dict) -> dict:
    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    return json.loads(asyncio.run(main.recall_webhook(FakeRequest())).body)


def _mute(monkeypatch, spoken: list) -> None:
    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)


def test_turn_latency_line_is_emitted_without_pii(tmp_path, monkeypatch, capsys):
    s = _session(tmp_path, monkeypatch)
    spoken: list = []
    _mute(monkeypatch, spoken)

    def gen(*a, **k):
        # populate the meta out-param the way the real engine does
        meta = k.get("meta")
        if meta is not None:
            meta.update({"rag_skipped": False, "retrieve_ms": 12.0,
                         "first_token_ms": 34.0, "web_search": False,
                         "model": "gemma-4-31b", "own_chars": 40,
                         "hist_chars": 80, "questions_chars": 20})
        yield "The DPA comes right after the security review."

    monkeypatch.setattr(main, "answer_question_stream", gen)
    body = _post(_line(s.bot_id, "Duccio", 1, "Laura, what is the DPA step?"))
    assert body.get("spoke") is True
    out = capsys.readouterr().out
    assert "[latency] turn" in out
    line = [ln for ln in out.splitlines() if "[latency] turn" in ln][-1]
    # Structured fields present…
    for field in ("total=", "retrieve=", "first_token=", "web_search=",
                  "cancelled=", "duplicated=", "model="):
        assert field in line
    # …and NO transcript text leaked into the log.
    assert "DPA" not in line
    assert "security review" not in line


# ── REPEAT GUARD: a short follow-up must not replay the last answer ───────────
_PREV = "The DPA comes right after the security review, and legal signs it off."


def test_short_followup_does_not_replay_last_answer(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    # Seed her previous turn as the anchor the guard compares against.
    s.add_utterance(speaker="Laura", text=_PREV, speaker_kind="agent")
    spoken: list = []
    _mute(monkeypatch, spoken)

    def replay(*a, **k):  # the model regurgitates the same answer verbatim
        yield _PREV

    monkeypatch.setattr(main, "answer_question_stream", replay)
    body = _post(_line(s.bot_id, "Duccio", 1, "Laura, and before that?"))
    # A short opening ack ("Good one —") may precede any answer — that's not the
    # replay. What must NOT happen is her previous multi-sentence ANSWER being
    # spoken again, and the turn must report the suppression honestly.
    assert _PREV not in spoken, f"replayed the previous answer: {spoken}"
    assert body.get("spoke") is False
    assert body.get("reason") == "duplicate answer suppressed"


def test_short_followup_with_a_novel_answer_still_speaks(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    s.add_utterance(speaker="Laura", text=_PREV, speaker_kind="agent")
    spoken: list = []
    _mute(monkeypatch, spoken)

    def novel(*a, **k):  # a genuinely different answer must NOT be suppressed
        yield "Before that you asked which tools I can use."

    monkeypatch.setattr(main, "answer_question_stream", novel)
    body = _post(_line(s.bot_id, "Duccio", 1, "Laura, and before that?"))
    # The novel answer is NOT a replay, so it must be spoken (the guard only
    # catches near-verbatim repeats of her last turn).
    assert "Before that you asked which tools I can use." in spoken
    assert _PREV not in spoken
    assert body.get("spoke") is True
