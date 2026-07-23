"""Regression coverage for canonical clarification binding.

Pure and key-free: no provider call, no model call, no transcript persistence.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.actions import action_plane, pending


def test_email_fragments_bind_to_one_pending_action_and_require_confirmation():
    item = {
        "action_id": "email-one",
        "action": "Can you create an email?",
        "owner": "",
        "due": "",
    }
    state = pending.create(
        "meeting-1",
        "speaker-1",
        item,
        ["email_to", "email_body"],
        question="Who should it go to, and what should it say?",
        kind="email",
    )
    assert state.key == "meeting-1:speaker-1:email-one"
    assert state.to_dict()["expected_answer_field"] == "email_to"

    bound, edit, missing, _ = pending.bind(state, "Send it to Anant.")
    assert bound is True
    item.update(edit)
    state.item = item
    assert missing == ["email_to_domain", "email_body"]
    assert item["action_id"] == "email-one"

    bound, edit, missing, question = pending.bind(
        state, "At s f f studio dot com."
    )
    assert bound is True
    item.update(edit)
    state.item = item
    assert missing == ["email_to_confirmation", "email_body"]
    assert "anant@sffstudio.com" in question
    assert "anant@sffstudio.com" not in item["action"]
    assert state.collected_parameters["recipient_confirmed"] is False

    bound, edit, missing, _ = pending.bind(
        state, "The body should say the demo is ready."
    )
    assert bound is True
    item.update(edit)
    state.item = item
    assert missing == ["email_to_confirmation"]
    assert "Recipient: Anant (address unconfirmed)" in item["action"]
    assert "Body: the demo is ready" in item["action"]
    assert state.status == "needs_details"
    assert state.action_id == "email-one"

    # No external address becomes executable until explicit confirmation.
    bound, edit, missing, _ = pending.bind(state, "Yes.")
    assert bound is True and missing == []
    item.update(edit)
    assert state.collected_parameters["to"] == ["anant@sffstudio.com"]
    assert state.status == "proposed"


def test_orphan_fragments_are_not_standalone_actions():
    for fragment in (
        "and send it to Anant",
        "at s f f studio dot com",
        "due Friday",
        "in the Northwind project",
    ):
        assert pending.is_orphan_fragment(fragment)


def test_snapshot_question_does_not_fill_pending_task_name():
    item = {
        "action_id": "task-one",
        "action": "Can you create a new task in Asana?",
    }
    state = pending.create(
        "meeting-2", "speaker-2", item, ["task_name"],
        question="What should the task be called?", kind="task",
    )
    bound, edit, missing, _ = pending.bind(
        state, "Do you have a snapshot of my Asana?"
    )
    assert bound is False and edit == {} and missing == ["task_name"]

    bound, edit, missing, _ = pending.bind(
        state, "Call the task QA TEST Pipedream routing."
    )
    assert bound is True and missing == []
    assert edit["action"].endswith("Task name: QA TEST Pipedream routing")
    assert state.collected_parameters["name"] == "QA TEST Pipedream routing"


def test_placeholder_task_names_remain_missing():
    for title in (
        "Task", "New task", "Create task", "Asana task", "",
        "Can you create a new task in Asana?",
    ):
        typed = {"type": "asana.create_task", "args": {"name": title}}
        assert action_plane.missing_params(typed) == ["name"]

    typed = {
        "type": "asana.create_task",
        "args": {"name": "QA TEST Pipedream routing"},
    }
    assert action_plane.missing_params(typed) == []
