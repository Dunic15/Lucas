"""Regression coverage for the 2026-07-22 live-brain quality pass.

All inputs are synthetic. No vendor calls, credentials or transcript logging.
"""
from __future__ import annotations

from app import store
from app.brain import engine, tool_registry, tools
from app.config import settings
from app import main as main_module


def test_self_referential_capability_questions_are_about_intent():
    for text in (
        "Can you read my Google Drive?",
        "Do you have access to my calendar?",
        "Can you hear me?",
        "Puoi leggere il mio Drive?",
        "Mi senti?",
    ):
        assert engine._is_about_avatar(text), text


def test_self_referential_questions_never_web_search(monkeypatch):
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "synthetic")
    for text in (
        "Can you read my current Google Drive?",
        "Do you have access to my calendar right now?",
        "Can you hear me today?",
    ):
        assert engine.wants_web_search(text) is False


def test_live_frame_guard_is_in_the_spoken_system_prompt():
    prompt = engine.ANSWER_STREAM_SYSTEM.format(
        persona="Synthetic avatar", name="Petra"
    )
    assert "CURRENT live turn" in prompt
    assert "never a pasted transcript" in prompt
    assert "first person as the avatar" in prompt


def test_grounding_honesty_guards_are_in_the_spoken_system_prompt():
    """Bug ② (live 2026-07-23): entity lookups by name must answer from context
    only ('I don't see Contoso in your Asana'), never pad with unrelated tasks
    or imply access; a short source-named follow-up refines the prior question.
    Guard the two clauses so they can't silently regress out of the prompt."""
    prompt = engine.ANSWER_STREAM_SYSTEM.format(
        persona="Synthetic avatar", name="Petra"
    )
    # entity-lookup honesty
    assert "Looking something up by NAME" in prompt
    assert "do NOT pad the answer with unrelated" in prompt
    assert 'imply you searched or "have access"' in prompt
    # follow-up refinement (Contoso → "in my Asana?")
    assert "REFINES the previous question" in prompt
    assert "is Contoso in my Asana" in prompt


def test_capture_filter_rejects_question_fragments_but_keeps_real_asks():
    assert not engine.wants_action_capture(
        "Schedule in my calendar? What schedule?"
    )
    assert not engine.wants_action_capture(
        "can you send can you schedule what what are in my calendar"
    )
    assert engine.wants_action_capture(
        "Can you schedule a meeting with Ana tomorrow at 3 PM?"
    )
    assert engine.wants_action_capture(
        "Email Jacopo saying hi"
    )


def test_clarify_detail_is_folded_into_an_explicit_slot():
    calendar = tools.fold_action_details(
        {"action": "Schedule the design review tomorrow with Ana"},
        "Three pm",
        ["invite_when"],
    )
    assert calendar["action"].endswith("When: Three pm")
    assert "at PM" not in calendar["action"]

    email = tools.fold_action_details(
        {"action": "Send an email to Jacopo"},
        "Saying hi",
        ["email_body"],
    )
    assert email["action"].endswith("Body: Saying hi")


def test_slack_linked_wording_names_cedric_even_without_connectors():
    reg = {
        "native": [],
        "cedric": {
            "linked": True,
            "connected": [],
            "available": [],
            "not_linked": False,
        },
        "slack_blocked": False,
        "pd_apps": [],
        "pd_org_available": [],
        "knowledge": {"docs": True, "drive_folder": False},
    }
    brief = tool_registry.brief(reg)
    assert "Slack: connected to this org" in brief
    assert "runs through Cedric" in brief
    result = tool_registry.search(reg, "slack")
    assert "connected to this org" in result
    assert "Cedric" in result


def test_recall_final_dedupe_is_sliding_and_pii_free():
    session = store.Session(
        bot_id="bot-synthetic", meeting_url="https://meet.example/test"
    )
    session._persist_enabled = False

    assert not main_module._is_duplicate_recall_final(
        session, "event-a", "fingerprint-a", now=100.0
    )
    # Different delivery id, same speaker/text fingerprint: duplicate.
    assert main_module._is_duplicate_recall_final(
        session, "event-b", "fingerprint-a", now=102.0
    )
    # A duplicate refreshes the window, suppressing a continuing replay loop.
    assert main_module._is_duplicate_recall_final(
        session, "event-c", "fingerprint-a", now=109.0
    )
    # A different final breaks the consecutive replay run. The same phrase is
    # then a meaningful A → B → A sequence, not a provider duplicate.
    assert not main_module._is_duplicate_recall_final(
        session, "event-x", "fingerprint-b", now=110.0
    )
    assert not main_module._is_duplicate_recall_final(
        session, "event-d", "fingerprint-a", now=111.0
    )
    # A real pause accepts the same spoken phrase again.
    assert not main_module._is_duplicate_recall_final(
        session, "event-e", "fingerprint-a", now=120.0
    )
    seen = session._recall_final_dedupe
    assert seen and all(key.startswith("e:") for key in seen)
    assert session._recall_final_last[0] == "fingerprint-a"


def test_no_false_refusal_when_snapshot_present():
    """Bug (live 2026-07-23): Petra read one person's Asana snapshot but told
    another she had 'no access' to the same workspace. The prompt must forbid
    that false refusal when a snapshot/brief is in context."""
    prompt = engine.ANSWER_STREAM_SYSTEM.format(persona="P", name="Petra")
    assert "you HAVE access to it" in prompt
    assert "false refusal" in prompt
    assert "isn't connected" in prompt  # the ONLY honest no-access wording
