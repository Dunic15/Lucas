"""Full-task-spec capture (owner ask 2026-07-21): the voice loop asks for the
decision fields (owner/project/due/description), the Action Centre form carries
subtasks/dependencies/attachments, and Approve actually applies them."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main as main  # noqa: E402
from app.brain import tools  # noqa: E402
from app.actions import action_plane  # noqa: E402
from app.integrations import asana_client  # noqa: E402
from app import pipedream_executor  # noqa: E402


def test_description_joins_the_clarify_slots():
    missing = tools.missing_action_details("create a task called kickoff")
    assert missing == ["owner", "project", "due", "description"]
    missing = tools.missing_action_details(
        "create a task called kickoff, assigned to Dana, in the launch "
        "project, due Friday, the description should say prep the deck"
    )
    assert missing == []


def test_clarify_line_asks_for_description():
    line = main._clarify_line("create a task", ["owner", "description"])
    assert "who should own it" in line and "description" in line


def test_schema_carries_full_field_set():
    fields = {f["name"] for f in action_plane.PARAMS_SCHEMAS["asana.create_task"]}
    assert {"name", "notes", "project", "assignee", "due_on",
            "subtasks", "dependencies", "attachments"} <= fields


def test_queue_confirmation_points_taskish_asks_at_the_card():
    task_line = main._queue_line_for("create a task", {"action": "create a task for X"})
    assert "subtask" in task_line.lower() or "sottoattiv" in task_line.lower()
    other = main._queue_line_for("send it", {"action": "send the recap to Marco"})
    assert other in main._QUEUE_LINES + main._QUEUE_LINES_IT


def test_native_create_applies_extras(monkeypatch):
    calls = []
    monkeypatch.setattr(asana_client, "_token", lambda org: ("pat", ""))
    monkeypatch.setattr(asana_client, "_workspace_gid", lambda pat: ("ws9", ""))
    monkeypatch.setattr(asana_client, "_resolve_project", lambda org, p: ("", ""))
    monkeypatch.setattr(
        asana_client, "_get",
        lambda pat, path, params=None: ([{"gid": "777"}], ""),
    )

    def fake_post(pat, path, body, *, params=None, what="request", method="POST"):
        calls.append((path, dict(body)))
        return {"ok": True, "task_gid": "42", "task_url": "https://asana/42", "name": "t"}

    monkeypatch.setattr(asana_client, "_post", fake_post)
    r = asana_client.create_task("org-x", {
        "name": "Kickoff", "subtasks": ["draft agenda", "book room"],
        "dependencies": ["research plan"], "attachments": ["https://x.com/f.pdf"],
    })
    assert r["ok"] and r["task_gid"] == "42"
    paths = [p for p, _ in calls]
    assert paths.count("/tasks/42/subtasks") == 2
    assert "/tasks/42/addDependencies" in paths
    assert "/attachments" in paths
    dep_body = dict(calls[paths.index("/tasks/42/addDependencies")][1])
    assert dep_body == {"dependencies": ["777"]}
    assert "applied" in r.get("extras", "")


def test_pipedream_create_carries_extras_in_notes():
    method, url, payload, _ = pipedream_executor._build_asana_create(
        "org-x", "acct", {
            "name": "Kickoff", "project": "123",
            "subtasks": ["draft agenda"], "dependencies": ["research plan"],
            "attachments": ["https://x.com/f.pdf"],
        })
    notes = payload["data"]["notes"]
    assert "Subtasks: draft agenda" in notes
    assert "Depends on: research plan" in notes
    assert "Attachments: https://x.com/f.pdf" in notes
