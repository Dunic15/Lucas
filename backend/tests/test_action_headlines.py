"""Gist headlines for actions (engine.headline_actions): a short imperative
`title` distilled from each action's raw `item`, so the Action Centre reads as
a task list instead of regurgitated transcript. Live repro 2026-07-23: post-
meeting action titles were raw transcript fragments ("Email that is different.
Hello?. Details:...").

Key-free: the LLM is monkeypatched; the stub provider must add no title.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import engine as brain  # noqa: E402
from app.brain import llm  # noqa: E402
from app.config import settings  # noqa: E402


def _fake_llm(mapping_json: str):
    def _complete(system, user, *, max_tokens=800, model=None, provider=None):
        return mapping_json
    return _complete


# ── stub provider: no model, no title (dashboard cleanup handles it) ──

def test_stub_provider_adds_no_title(monkeypatch):
    monkeypatch.setattr(settings, "brain_provider", "stub")
    monkeypatch.setattr(settings, "brain_provider_post", "")
    assert brain.post_provider() == "stub"
    out = brain.headline_actions([
        {"item": "Can you send an email to Duccio please. Still talking."},
    ])
    assert "title" not in out[0]


# ── real provider: distilled headline applied, item untouched ──

def test_headline_applied_and_item_preserved(monkeypatch):
    monkeypatch.setattr(
        llm, "complete",
        _fake_llm('{"0": "Email Duccio the meeting recap", '
                  '"1": "Schedule a follow-up with Marco"}'),
    )
    raw_item = "Can you send an email to Duccio please. Still talking."
    out = brain.headline_actions(
        [{"item": raw_item}, {"item": "so, uh, schedule a follow-up with Marco"}],
        provider="anthropic",
    )
    assert out[0]["title"] == "Email Duccio the meeting recap"
    assert out[0]["item"] == raw_item  # verbatim evidence never rewritten
    assert out[1]["title"] == "Schedule a follow-up with Marco"


def test_headline_sanitized(monkeypatch):
    monkeypatch.setattr(
        llm, "complete",
        _fake_llm('{"0": "  Email  Duccio the recap.  "}'),
    )
    out = brain.headline_actions([{"item": "email duccio"}], provider="anthropic")
    assert out[0]["title"] == "Email Duccio the recap"  # collapsed + trailing . stripped


def test_index_keys_may_be_ints(monkeypatch):
    # A model that emits integer JSON keys (some do) still binds correctly.
    monkeypatch.setattr(llm, "complete", _fake_llm('{"0": "Book the room"}'))
    out = brain.headline_actions([{"item": "can you book the room"}],
                                 provider="anthropic")
    assert out[0]["title"] == "Book the room"


# ── best-effort: never breaks finalize ──

def test_malformed_model_output_leaves_actions_unchanged(monkeypatch):
    monkeypatch.setattr(llm, "complete", _fake_llm("not json at all"))
    out = brain.headline_actions([{"item": "email duccio"}], provider="anthropic")
    assert "title" not in out[0]


def test_llm_exception_is_swallowed(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(llm, "complete", _boom)
    out = brain.headline_actions([{"item": "email duccio"}], provider="anthropic")
    assert "title" not in out[0]


# ── shape safety ──

def test_non_dict_and_empty_items_pass_through(monkeypatch):
    monkeypatch.setattr(llm, "complete", _fake_llm('{"0": "X", "2": "Book room"}'))
    actions = ["a bare string", {"item": ""}, {"item": "book the room"}]
    out = brain.headline_actions(actions, provider="anthropic")
    assert out[0] == "a bare string"           # non-dict untouched
    assert "title" not in out[1]               # empty item, no index in prompt
    assert out[2]["title"] == "Book room"


def test_empty_and_none_are_safe(monkeypatch):
    monkeypatch.setattr(llm, "complete", _fake_llm("{}"))
    assert brain.headline_actions([], provider="anthropic") == []
    assert brain.headline_actions(None, provider="anthropic") == []


def test_returns_new_list_without_mutating_input(monkeypatch):
    monkeypatch.setattr(llm, "complete", _fake_llm('{"0": "Book room"}'))
    original = {"item": "book the room"}
    out = brain.headline_actions([original], provider="anthropic")
    assert "title" not in original             # input dict not mutated
    assert out[0]["title"] == "Book room"


# ── dashboard read prefers the persisted headline ──

def test_dashboard_prefers_persisted_title_over_display_cleanup():
    from app.api.dashboard import _action_entry

    # With a finalize-time headline: it wins over _display_title.
    with_headline = _action_entry({
        "action_id": "a1",
        "item": "Patrick, can you send an email to please?",
        "title": "Email Duccio the recap",
    })
    assert with_headline["title"] == "Email Duccio the recap"

    # Without one (pre-headline artifact): the deterministic cleanup still runs.
    without = _action_entry({
        "action_id": "a2",
        "item": "Patrick, can you send an email to please?",
    })
    assert without["title"] == "Send an email to please"
