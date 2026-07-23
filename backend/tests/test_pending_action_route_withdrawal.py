"""Regression coverage for pending-action binding, current routing and withdrawal.

All scenarios are key-free: provider calls are faked and no external object is created.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main as main_module
from app import auth, avatar_resolver, executor, ledger, pipedream_executor, store
from app.actions import action_plane
from app.api import dashboard as dash
from app.brain import tools as brain_tools
from app.config import settings
from app.persistence import audit_log


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    dash._syscheck_last.clear()
    return TestClient(main_module.app)


def _login(client: TestClient) -> dict:
    user = store.upsert_user("owner@x.com")
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def _seed_action(org: str, action: dict, *, bot_id: str) -> None:
    store.save_artifact(
        bot_id,
        {
            "summary": "fixture",
            "actions": [action],
            "checklist": [action],
            "org_id": org,
            "avatar_id": "laura",
            "meeting_url": "https://meet.google.com/action-fixture",
            "transcript": "",
        },
        org_id=org,
    )


def test_email_clarifications_bind_to_one_action_and_require_confirmation():
    action = {
        "action_id": "emailaction00001",
        "action": "Can you create an email?",
    }
    one_card = [action]

    missing = brain_tools.missing_action_details(action["action"], "email")
    assert missing == ["email_to", "email_body"]

    assert brain_tools.clarification_fragment_matches(
        "Send it to Anant.", "email", missing
    )
    action.update(brain_tools.fold_action_details(action, "Send it to Anant.", missing))
    assert action["action_id"] == "emailaction00001"
    assert brain_tools.missing_action_details(action["action"], "email") == [
        "email_to", "email_body"
    ]

    action.update(
        brain_tools.fold_action_details(
            action,
            "At s f f studio dot com.",
            brain_tools.missing_action_details(action["action"], "email"),
        )
    )
    missing = brain_tools.missing_action_details(action["action"], "email")
    assert missing == ["email_to_confirm", "email_body"]
    assert "Recipient candidate: anant@sffstudio.com" in action["action"]

    action.update(
        brain_tools.fold_action_details(
            action,
            "The body should say the demo is ready.",
            missing,
        )
    )
    missing = brain_tools.missing_action_details(action["action"], "email")
    assert missing == ["email_to_confirm"]
    assert "Body: the demo is ready" in action["action"]

    # The proposed address remains non-executable until the speaker confirms it.
    assert "Recipient: anant@sffstudio.com" not in action["action"]
    action.update(brain_tools.fold_action_details(action, "Yes", missing))
    assert brain_tools.missing_action_details(action["action"], "email") == []
    params = brain_tools.collected_action_parameters(action["action"])
    assert params["recipient"] == "anant@sffstudio.com"
    assert params["body"] == "the demo is ready"
    assert len(one_card) == 1
    assert one_card[0]["action_id"] == "emailaction00001"
    assert "tracked only" not in action["action"].lower()


@pytest.mark.parametrize(
    "name",
    ["", "Task", "New task", "Create task", "Asana task", "Create a new task"],
)
def test_placeholder_asana_names_remain_needs_details(name):
    typed = {"type": "asana.create_task", "args": {"name": name}}
    assert action_plane.missing_params(typed) == ["name"]


def test_asana_title_binding_skips_snapshot_question_and_optional_metadata():
    action = {
        "action_id": "asanaction000001",
        "action": "Can you create a new task in Asana?",
    }
    missing = brain_tools.missing_action_details(action["action"], "task")
    assert missing == ["task_name"]
    assert not brain_tools.clarification_fragment_matches(
        "Do you have a snapshot of my Asana?", "task", missing
    )

    action.update(
        brain_tools.fold_action_details(
            action, "Call the task QA TEST Pipedream routing.", missing
        )
    )
    assert "Task name: QA TEST Pipedream routing" in action["action"]
    assert "snapshot" not in action["action"].lower()
    assert brain_tools.missing_action_details(action["action"], "task") == []

    typed = {
        "type": "asana.create_task",
        "args": {"name": "QA TEST Pipedream routing"},
    }
    assert action_plane.missing_params(typed) == []


def test_stale_asana_route_displays_and_executes_only_pipedream(
    client, monkeypatch
):
    user = _login(client)
    aid = "routeaction00001"
    _seed_action(
        user["org_id"],
        {
            "action_id": aid,
            "item": "Create QA TEST Pipedream routing",
            "typed": {
                "type": "asana.create_task",
                "args": {"name": "QA TEST Pipedream routing"},
            },
            "execution_route": "manual",
        },
        bot_id="bot-route-fixture",
    )

    monkeypatch.setattr(settings, "pipedream_executor", True)
    monkeypatch.setattr(settings, "pipedream_project_id", "proj-test")
    monkeypatch.setattr(settings, "pipedream_client_secret", "secret-test")
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(settings, "action_dispatch_async", False)
    monkeypatch.setattr(
        pipedream_executor, "app_connected",
        lambda org, app: org == user["org_id"] and app == "asana",
    )
    monkeypatch.setattr(store, "get_avatar_capabilities", lambda *a, **k: {})
    monkeypatch.setattr(avatar_resolver, "family_allowed", lambda *a, **k: True)

    calls = {"pipedream": 0, "native": 0}

    def fake_pipedream(org, action_id, action):
        calls["pipedream"] += 1
        ledger.set_action_status(
            action_id,
            "done",
            "Pipedream · task gid-test",
            org_id=org,
            receipt={
                "provider": "pipedream",
                "external_id": "gid-test",
                "url": "https://app.asana.com/0/0/gid-test/f",
            },
        )
        return {
            "ok": True,
            "provider": "pipedream",
            "external_id": "gid-test",
        }

    def fake_native(*args, **kwargs):
        calls["native"] += 1
        raise AssertionError("native Asana must not be called")

    monkeypatch.setattr(pipedream_executor, "execute_approved", fake_pipedream)
    monkeypatch.setattr(executor, "execute_approved", fake_native)

    shown = client.get(f"/dashboard/actions/{aid}")
    assert shown.status_code == 200
    assert shown.json()["action"]["route"] == "pipedream"

    first = client.post(
        f"/dashboard/actions/{aid}/approve",
        headers={"sec-fetch-site": "same-origin"},
    )
    assert first.status_code == 200
    assert first.json()["executed"] is True
    assert calls == {"pipedream": 1, "native": 0}

    # A replay sees the existing decision/terminal receipt and cannot write twice.
    replay = client.post(
        f"/dashboard/actions/{aid}/approve",
        headers={"sec-fetch-site": "same-origin"},
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert calls == {"pipedream": 1, "native": 0}
    status = ledger.action_statuses([aid], org_id=user["org_id"])[aid]
    assert status["status"] == "done"
    assert "Pipedream" in status["detail"]


def test_discard_soft_withdraws_preserves_history_and_blocks_approval(
    client, monkeypatch
):
    user = _login(client)
    aid = "withdrawaction01"
    _seed_action(
        user["org_id"],
        {
            "action_id": aid,
            "item": "Can you create an email?",
            "typed": {
                "type": "email.send",
                "args": {"subject": "Demo"},
            },
            "execution_route": "manual",
        },
        bot_id="bot-withdraw-fixture",
    )

    writes = {"pipedream": 0, "native": 0}
    monkeypatch.setattr(
        pipedream_executor,
        "execute_approved",
        lambda *a, **k: writes.update(pipedream=writes["pipedream"] + 1),
    )
    monkeypatch.setattr(
        executor,
        "execute_approved",
        lambda *a, **k: writes.update(native=writes["native"] + 1),
    )
    audited = []
    monkeypatch.setattr(
        audit_log, "record",
        lambda org, **fields: audited.append((org, fields)),
    )

    withdrawn = client.post(
        f"/dashboard/actions/{aid}/withdraw",
        headers={"sec-fetch-site": "same-origin"},
        json={"confirm_approved": False},
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"]["status"] == "withdrawn"

    # Refresh/detail reads preserve the action and expose the terminal history state.
    detail = client.get(f"/dashboard/actions/{aid}")
    assert detail.status_code == 200
    assert detail.json()["action"]["status"] == "withdrawn"
    summary = client.get("/dashboard/summary").json()
    rows = [
        action
        for meeting in summary.get("meetings", [])
        for action in meeting.get("actions", [])
        if action.get("action_id") == aid
    ]
    assert len(rows) == 1
    assert rows[0]["execution"]["status"] == "withdrawn"

    refused = client.post(
        f"/dashboard/actions/{aid}/approve",
        headers={"sec-fetch-site": "same-origin"},
    )
    assert refused.status_code == 409
    assert writes == {"pipedream": 0, "native": 0}
    assert any(fields.get("action") == "action.withdraw" for _, fields in audited)

    replay = client.post(
        f"/dashboard/actions/{aid}/withdraw",
        headers={"sec-fetch-site": "same-origin"},
        json={"confirm_approved": False},
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True


def test_action_centre_fixture_exposes_discard_and_history_bucket():
    html = (
        Path(__file__).resolve().parents[2] / "frontend" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert 'data-withdraw="' in html
    assert '/withdraw"' in html
    assert 's==="withdrawn") return "done"' in html
    assert 'withdrawn:"Withdrawn"' in html
    assert "Action withdrawn — preserved in history." in html
