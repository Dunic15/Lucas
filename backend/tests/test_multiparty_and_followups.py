"""Multiparty fluidity + team follow-ups (live 3-person call, 2026-07-24).

Four failures from one meeting:
P1 "can you check my calendar and my next meeting?" was IGNORED (no wake word
   with 3 humans — gating was blind to content);
P2 "…when I tested it this week… can you get the snapshot now?" (ASR-fused)
   hit the 'this week' freshness trigger and got a surreal public-web answer;
P3 the capability line promised "I can pull one" but no snapshot pull existed;
P4 "Send Cedric the product link / the demo / the YC video" — participants
   talking to EACH OTHER — surfaced as Needs-details email cards.
Key-free.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import engine  # noqa: E402
from app.config import settings  # noqa: E402
from app.meeting.lifecycle import _assistant_request, _merge_action_items  # noqa: E402


# ── P4: human commitments are notes, never executable cards ─────────────
def test_assistant_flag_from_model_wins():
    assert _assistant_request({"item": "X", "assistant": True}, "Petra")
    assert not _assistant_request({"item": "X", "assistant": False}, "Petra")


def test_human_commitment_heuristics():
    # participants talking to each other → note ("Cedric" here is a HUMAN)
    assert not _assistant_request(
        {"item": "Send Cedric the product link to test",
         "evidence": "we can also send you the link, and you can test it"},
        "Petra",
    )
    # a spoken ask naming the avatar → assistant request
    assert _assistant_request(
        {"item": "Send the recap to marco@acme.com",
         "evidence": "Petra, can you send the recap to marco at acme dot com"},
        "Petra",
    )
    # spoken-ask shape without the name still counts (1:1 style ask)
    assert _assistant_request(
        {"item": "Schedule a follow-up",
         "evidence": "can you schedule a follow-up with Marco"},
        "Petra",
    )
    # no evidence of a direct ask → note
    assert not _assistant_request({"item": "Update the roadmap deck"}, "Petra")


def test_merge_marks_human_followups():
    merged = _merge_action_items(
        [],  # no live captures
        [{"item": "Send Cedric the YC application demo video",
          "owner": "Ananth Iyer",
          "evidence": "we'll send that to you as well"},
         {"item": "Send the recap to marco@acme.com",
          "owner": "UNASSIGNED",
          "evidence": "Petra, please send the recap to marco@acme.com"}],
        "Petra",
    )
    by_item = {m["item"]: m for m in merged}
    assert by_item["Send Cedric the YC application demo video"].get(
        "human_followup") is True
    assert "human_followup" not in by_item["Send the recap to marco@acme.com"]


def test_live_captures_never_flagged():
    merged = _merge_action_items(
        [{"action": "send the recap to marco@acme.com", "action_id": "a1"}],
        [], "Petra",
    )
    assert "human_followup" not in merged[0]
    assert merged[0]["requested_live"] is True


def test_human_followups_are_never_typed():
    out = engine.type_actions(
        [{"item": "Send Cedric the demo video", "human_followup": True},
         {"item": "Send the recap to dana@acme.com"}],
        provider="stub",
    )
    assert "typed" not in out[0]           # note stays a note
    assert out[1]["typed"]["type"] == "email.send"


def test_dashboard_state_for_team_followups():
    from app.api.dashboard import _action_needed, _card_state
    a = {"item": "Send Cedric the product link", "human_followup": True}
    assert _action_needed(a) == []          # nothing to interrogate
    assert _card_state(a) == "team_followup"


def test_extraction_prompt_carries_the_assistant_field():
    p = engine.POSTMEETING_SYSTEM
    assert '"assistant"' in p
    assert "assistant=false" in p or "assistant\": <true ONLY" in p


# ── P1: tool-domain asks count as addressed without the wake word ───────
@pytest.mark.parametrize("q", [
    "can you check my calendar and my next meeting that I have?",
    "what is on the calendar for tomorrow",
    "can you schedule a follow-up with Marco",
    "pull my inbox",
])
def test_tool_domain_asks_are_addressed(q):
    assert engine.is_tool_domain_ask(q) is True


@pytest.mark.parametrize("q", [
    "I am thinking whom I can pitch this to",
    "the economy is awaiting a bubble pop",
    "we can also send you the link and you can test it",
    "it's super fascinating what's happening in the AI era",
])
def test_human_chatter_is_not_addressed(q):
    assert engine.is_tool_domain_ask(q) is False


# ── P2: snapshot asks never web-search (even ASR-fused with 'this week') ─
def test_snapshot_asks_never_search(monkeypatch):
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "synthetic")
    for q in ("when I tested it this week, can you get the snapshot now?",
              "check asana and get a snapshot of the asana tasks"):
        assert engine.wants_web_search(q) is False, q


# ── P3: the pull trigger is explicit-verb only ───────────────────────────
def test_snapshot_pull_trigger_shapes():
    import app.main as main
    yes = ("Petra, can you get the snapshot now?",
           "Check Asana and get a snapshot of the Asana tasks, Petra.",
           "pull my inbox", "refresh my asana")
    no = ("do you have a snapshot of my asana?",   # capability, not a pull
          "the snapshot was empty yesterday")
    for q in yes:
        assert main._SNAPSHOT_PULL.search(q), q
    for q in no:
        assert not main._SNAPSHOT_PULL.search(q), q
