"""Regression coverage for a Notion write requested during a meeting.

The live failure classified "summary of this meeting" as Calendar, appended
email Subject/Body fields, and made Cedric deny a connected Notion workspace.
All tests are deterministic and vendor-free.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.actions import executor  # noqa: E402
from app.api import voice_agent  # noqa: E402
from app.brain import capabilities, engine, tools  # noqa: E402


ASK = (
    "Create a Notion page called OpenClaw meeting workflow test and then add "
    "a short summary of this meeting and include a checklist with the next "
    "three steps to do."
)
BRIEF = "The team agreed to test the approval-gated Notion workflow."


def test_notion_page_request_is_not_calendar_or_email():
    assert tools.ask_kind(ASK) == "notion"
    assert tools.missing_action_details(ASK, tools.ask_kind(ASK)) == []
    action = engine.prefill_summary_emails([{"item": ASK}], BRIEF)[0]
    assert "Subject:" not in action["item"]
    assert "Body:" not in action["item"]


def test_notion_page_request_types_with_title_summary_and_checklist():
    typed = engine.type_actions(
        [{"item": ASK}], BRIEF, provider="stub"
    )[0]["typed"]
    assert typed["type"] == "notion.create_page"
    assert typed["args"]["title"] == "OpenClaw meeting workflow test"
    assert BRIEF in typed["args"]["content"]
    assert typed["args"]["content"].count("- [ ]") == 3
    assert "start" not in typed["args"] and "end" not in typed["args"]


def test_notion_intent_overrides_model_calendar_guess(monkeypatch):
    monkeypatch.setattr(
        engine,
        "_llm_type_actions",
        lambda *_a, **_k: {
            0: {
                "type": "calendar.create_event",
                "args": {
                    "title": "Wrong",
                    "start": "2026-08-01T15:00:00",
                    "end": "2026-08-01T15:30:00",
                },
            }
        },
    )
    typed = engine.type_actions(
        [{"item": ASK}], BRIEF, provider="synthetic-model"
    )[0]["typed"]
    assert typed["type"] == "notion.create_page"


def test_notion_uses_its_own_capability_family_and_executor_shape(monkeypatch):
    from app import pipedream_executor

    typed = {
        "type": "notion.create_page",
        "args": {"title": "Workflow test", "content": "Synthetic"},
    }
    monkeypatch.setattr(pipedream_executor, "enabled", lambda: True)
    monkeypatch.setattr(pipedream_executor, "handles", lambda _a: True)
    assert executor.capability_family(typed["type"]) == "notion"
    assert executor.from_typed(typed) == {
        "type": "notion.create_page",
        "args": typed["args"],
    }


def test_connected_notion_is_present_in_spoken_capability_truth(monkeypatch):
    monkeypatch.setattr(
        executor, "route_for_typed", lambda *_a, **_k: "openclaw"
    )
    session = SimpleNamespace(
        asana_live=False,
        tool_registry={
            "native": [],
            "pd_apps": [{"slug": "notion", "actions": ["create page"]}],
            "pd_org_available": [],
        },
    )
    snap = capabilities.snapshot("cedric", "org-synthetic", session)
    notion = snap["tools"]["pd:notion"]
    assert notion["connected_for_org"] is True
    assert notion["enabled_for_avatar"] is True
    assert notion["can_execute_now"] is True
    answer = capabilities.answer("Can you create pages in Notion?", snap)
    assert "Notion is connected" in answer
    assert "OpenClaw after approval" in answer


def test_connected_but_disabled_notion_is_not_reported_as_disconnected():
    session = SimpleNamespace(
        asana_live=False,
        tool_registry={
            "native": [],
            "pd_apps": [],
            "pd_org_available": ["notion"],
        },
    )
    snap = capabilities.snapshot("cedric", "org-synthetic", session)
    answer = capabilities.answer("Is Notion connected?", snap)
    assert "Notion is connected for this workspace" in answer
    assert "isn't enabled for this agent" in answer
    assert "isn't connected" not in answer


def test_agent_prompt_queues_explicit_writes_without_capability_preflight():
    session = SimpleNamespace(
        meeting_url="https://meet.example/synthetic",
        integration={},
        asana_snapshot="",
        roster=lambda: [],
    )
    avatar = SimpleNamespace(
        name="Cedric",
        persona_prompt="You are Cedric.",
        voice_agent_language="en",
    )
    payload = voice_agent.build_init_payload(session, avatar)
    prompt = payload["conversation_config_override"]["agent"]["prompt"]["prompt"]
    assert "call queue_action IMMEDIATELY" in prompt
    assert "Do NOT call get_available_actions first" in prompt
    assert "Frustration, swearing or criticism is NOT a leave request" in prompt
    assert "Never introduce or continue a topic absent" in prompt

    from scripts import create_meeting_agent

    fallback = create_meeting_agent.PILOT_RULES
    assert "call queue_action IMMEDIATELY" in fallback
    assert "Do NOT call\n  get_available_actions first" in fallback
    assert "Frustration, swearing or criticism is NOT a leave request" in fallback
