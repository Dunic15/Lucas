"""Deterministic capability truth + grounding on the live path (2026-07-23).

Live test: within one meeting Petra claimed Asana/Jira/Calendar/Gmail/Drive
connected, then "I don't have access to your Asana", then "I can't access
external applications", and web-searched "which actions can you do in Gmail?".
These lock the fix: capability questions resolve to a deterministic org-scoped
answer (never web search, never a model-invented roster), and the answer keeps
connected / snapshot / executable as separate states. Key-free — no vendors.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import capabilities as cap  # noqa: E402
from app.brain import engine  # noqa: E402
from app.config import settings  # noqa: E402


# ── the exact regression transcript ─────────────────────────────────────
_REGRESSION = [
    "Which tools can you use?",
    "Which actions can you do in Google or in Gmail?",
    "Do you have a snapshot of my Asana?",
    "What's in my zone at the moment?",  # ASR of "in my Asana" (context-bound)
    "Do you have access to my Google Drive?",
    "What do you actually see in Drive?",
]


@pytest.mark.parametrize("utter", _REGRESSION[:3] + _REGRESSION[4:])
def test_capability_questions_are_classified(utter):
    # (index 3, the "zone" ASR line, is a workspace read after repair — covered
    # separately; the rest must all read as capability/self questions.)
    assert cap.is_capability_question(utter), utter


@pytest.mark.parametrize("utter", _REGRESSION)
def test_capability_utterances_never_web_search(monkeypatch, utter):
    """The whole sequence, incl. the exact 'which action … in Gmail' line, must
    NEVER trigger a public web search."""
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "synthetic")
    assert engine.wants_web_search(utter) is False, utter


def test_which_action_in_gmail_is_a_self_question(monkeypatch):
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "synthetic")
    q = "Which action can you do in Google or in Gmail?"
    assert cap.is_capability_question(q)
    assert engine._is_about_avatar(q)          # defense-in-depth gate
    assert engine.wants_web_search(q) is False  # never a web search


def test_ordinary_web_search_still_works(monkeypatch):
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "synthetic")
    for q in ("can you search online for the latest news",
              "look it up on the web", "what's the latest funding round for Acme"):
        assert not cap.is_capability_question(q), q


# ── the deterministic answer keeps the four states separate ─────────────
def _snap(**overrides):
    base = {
        "asana_tasks": dict(connected_for_org=True, enabled_for_avatar=True,
                            live_health="ok", snapshot_available_in_meeting=False,
                            can_read_now=False, can_execute_now=True,
                            execution_route="pipedream",
                            supported_verbs=["create tasks", "update tasks"],
                            unavailable_reason="no snapshot"),
        "gmail_send": dict(connected_for_org=True, enabled_for_avatar=True,
                           live_health="ok", snapshot_available_in_meeting=None,
                           can_read_now=True, can_execute_now=True,
                           execution_route="native",
                           supported_verbs=["send email"], unavailable_reason=""),
        "google_drive": dict(connected_for_org=False, enabled_for_avatar=False,
                             live_health="unconfigured",
                             snapshot_available_in_meeting=None, can_read_now=False,
                             can_execute_now=False, execution_route="",
                             supported_verbs=[], unavailable_reason="not connected"),
    }
    base.update(overrides)
    return {"tools": base}


def test_answer_separates_connected_from_snapshot():
    a = cap.answer("do you have a snapshot of my asana?", _snap()).lower()
    assert "asana is connected" in a          # (1) connected
    assert "create tasks" in a                # (4) can execute
    assert "snapshot" in a and "can't read" in a  # (3) no snapshot → honest
    # never the flat wrong claim
    assert "i don't have access to asana" not in a
    assert "can't access external" not in a


def test_answer_never_invents_a_tool():
    """No phantom Jira: the roster names only what's actually connected."""
    a = cap.answer("which tools can you use?", _snap()).lower()
    assert "jira" not in a
    assert "asana" in a and "gmail" in a
    assert "google drive" not in a  # drive is unconnected → not claimed


def test_answer_unconnected_tool_is_honest():
    a = cap.answer("can you read my drive?", _snap()).lower()
    assert "isn't connected" in a
    assert "i can see your drive" not in a  # never a fabricated inventory


# ── context-bound ASR repair ────────────────────────────────────────────
def test_asr_repair_only_with_supporting_context():
    # Asana just discussed → "my zone" is repaired.
    assert "asana" in cap.repair_asr(
        "what's in my zone at the moment?", "Duccio: can you check my asana"
    ).lower()
    # No Asana in context → left untouched (never a global rewrite).
    assert cap.repair_asr(
        "what's in my zone at the moment?", "Duccio: how's the weather"
    ) == "what's in my zone at the moment?"


# ── grounding rules present in the spoken prompt ────────────────────────
def test_grounding_state_distinctions_are_in_the_prompt():
    prompt = engine.ANSWER_STREAM_SYSTEM.format(persona="P", name="Petra")
    assert "Keep FOUR states separate" in prompt
    assert 'never "I don\'t have access to Asana"' in prompt
    assert 'Never say "the meeting hasn\'t started"' in prompt
    assert "no phantom Jira" in prompt


# ── A2: meta / self-STATE questions never web-search (421-bis) ───────────
# Live 2026-07-23: "why are you slower than yesterday?" hit the 'yesterday'
# search trigger and got a public web result; "what happened to you?" hit
# 'happened'. These are first-person answers about HER, never a lookup.
_SELF_STATE = [
    "why are you slower than yesterday?",
    "why are you so quiet today?",
    "are you frozen?",
    "are you still there?",
    "what happened to you?",
    "perché sei così lenta?",
    "ci sei?",
    "cosa ti è successo?",
]


@pytest.mark.parametrize("utter", _SELF_STATE)
def test_self_state_is_about_avatar_and_never_searches(monkeypatch, utter):
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "synthetic")
    assert engine._is_about_avatar(utter), utter          # routed to about/, not web
    assert engine.wants_web_search(utter) is False, utter  # NEVER a public search


def test_self_state_does_not_break_ordinary_time_searches(monkeypatch):
    """The self-state branch must not swallow a real 'what happened' news query."""
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "synthetic")
    # world question, not about her → still searches
    assert engine.wants_web_search("what happened in the markets today?") is True


# ── A1: capability snapshot is per-session stable (421-bis) ──────────────
# Source of truth is the registry assembled ONCE at join and stored on
# session.tool_registry (lifecycle.py) — the same object tools.py reads.
class _Sess:
    def __init__(self, asana_live=False, tool_registry=None):
        self.asana_live = asana_live
        if tool_registry is not None:
            self.tool_registry = tool_registry


def _good_registry():
    return {"native": [
        {"name": "asana_tasks", "connected": True, "write": True,
         "verbs": ["create tasks"]},
        {"name": "gmail_send", "connected": True, "write": True,
         "verbs": ["send email"]},
    ]}


def test_cached_snapshot_is_stable_when_registry_goes_missing(monkeypatch):
    """If the join registry is momentarily unavailable on a later turn, the
    cached good snapshot must be served — never a false 'nothing connected'
    (the exact flip-flop seen live 2026-07-23)."""
    from app.brain import tool_registry
    from app.actions import executor
    monkeypatch.setattr(executor, "route_for_typed", lambda typed, org: "pipedream")
    # A fresh re-assembly on the failing turn returns nothing (total failure).
    monkeypatch.setattr(tool_registry, "assemble", lambda org, av: None)

    sess = _Sess(tool_registry=_good_registry())
    first = cap.cached_snapshot("petra", "org-x", sess)
    assert first["tools"]["asana_tasks"]["connected_for_org"] is True
    # Registry gone AND the snapshot signal flipped (forces a recompute): the
    # fresh build is empty/failed, so the cached good snapshot is served.
    sess.tool_registry = None
    sess.asana_live = True
    second = cap.cached_snapshot("petra", "org-x", sess)
    assert second is first
    assert second["tools"]["asana_tasks"]["connected_for_org"] is True


def test_cached_snapshot_recomputes_when_snapshot_signal_flips(monkeypatch):
    """asana_live flips False→True when a workspace brief loads at join — the
    cache must refresh so the read state is honest."""
    from app.actions import executor
    monkeypatch.setattr(executor, "route_for_typed", lambda typed, org: "pipedream")
    reg = {"native": [
        {"name": "asana_tasks", "connected": True, "write": True,
         "verbs": ["create tasks"]},
    ]}
    sess = _Sess(asana_live=False, tool_registry=reg)
    s1 = cap.cached_snapshot("petra", "org-x", sess)
    assert s1["tools"]["asana_tasks"]["snapshot_available_in_meeting"] is False
    sess.asana_live = True
    s2 = cap.cached_snapshot("petra", "org-x", sess)
    assert s2 is not s1
    assert s2["tools"]["asana_tasks"]["snapshot_available_in_meeting"] is True


def test_snapshot_reports_ok_flag(monkeypatch):
    from app.brain import tool_registry
    from app.actions import executor
    monkeypatch.setattr(executor, "route_for_typed", lambda typed, org: "native")
    # A join-cached registry → ok True, real tools.
    good = cap.snapshot("petra", "org-x", _Sess(tool_registry=_good_registry()))
    assert good.get("ok") is True
    assert good["tools"]["asana_tasks"]["connected_for_org"] is True
    # No registry anywhere (session has none, assembly returns None) → ok False,
    # empty tools — an honest 'unknown', never a confident 'nothing connected'.
    monkeypatch.setattr(tool_registry, "assemble", lambda org, av: None)
    failed = cap.snapshot("petra", "org-x", _Sess())
    assert failed.get("ok") is False
    assert failed.get("tools") == {}


# ── A3: owner-refusal guard present in the spoken prompt (421-bis) ───────
def test_owner_refusal_rule_is_in_the_prompt():
    prompt = engine.ANSWER_STREAM_SYSTEM.format(persona="P", name="Petra")
    assert "you aren't the owner" in prompt
    assert "prove who they are" in prompt
    assert "that's not your account" in prompt
