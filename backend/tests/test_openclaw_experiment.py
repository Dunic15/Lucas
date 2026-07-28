from __future__ import annotations

import importlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main as main_module
from app import pipedream_executor, store
from app.actions import executor, ledger, outbox
from app.config import settings
from app.openclaw import gates, runtime


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    importlib.reload(outbox)
    runtime._SCHEMA_READY = False
    return store


@pytest.fixture
def active_openclaw(monkeypatch, fresh_store):
    org = settings.demo_org_id
    monkeypatch.setattr(settings, "openclaw_experiment_enabled", True)
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", org)
    monkeypatch.setattr(settings, "native_executor", True)
    return org


def _artifact(action_id: str = "oc_action_1") -> dict:
    return {
        "summary": "Customer asked for a follow-up.",
        "decisions": ["Proceed with the pilot."],
        "actions": [
            {
                "action_id": action_id,
                "item": "Email the customer the recap",
                "owner": "Laura",
                "typed": {
                    "type": "email.send",
                    "args": {
                        "to": ["customer@example.com"],
                        "subject": "Recap",
                        "body": "Thanks for meeting.",
                    },
                },
            }
        ],
        "avatar_id": "laura",
        "org_id": settings.demo_org_id,
        "meeting_url": "https://meet.google.com/open-claw-test",
        "transcript": "Customer: Please send the recap.",
    }


def _wait_until(fn, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.01)
    return fn()


def test_openclaw_gate_requires_global_flag_and_allowlist(monkeypatch):
    org = "00000000-0000-0000-0000-000000000123"
    monkeypatch.setattr(settings, "openclaw_experiment_enabled", False)
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", org)
    assert gates.experiment_enabled_for_org(org) is False

    monkeypatch.setattr(settings, "openclaw_experiment_enabled", True)
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", "")
    assert gates.experiment_enabled_for_org(org) is False

    monkeypatch.setattr(settings, "openclaw_experiment_orgs", f"other,{org}")
    assert gates.experiment_enabled_for_org(org) is True
    assert gates.experiment_enabled_for_org("other-org") is False


def test_openclaw_route_wins_for_allowlisted_org(active_openclaw):
    typed = {"type": "email.send", "args": {"to": ["a@b.com"], "subject": "S", "body": "B"}}
    assert executor.route_for_typed(typed, active_openclaw) == "openclaw"
    assert executor.effective_route(
        typed, active_openclaw, stored_route="native"
    ) == "openclaw"
    assert executor.route_for_typed(typed, "not-allowlisted") == "native"
    assert ledger.set_action_route("a1", "openclaw", org_id=active_openclaw)


def test_native_executor_is_suppressed_for_openclaw_org(active_openclaw, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        executor.native_runtime,
        "execute",
        lambda org, action: calls.append({"org": org, "action": action}) or {"ok": True},
    )

    result = executor.execute_approved(
        active_openclaw,
        "a1",
        {"type": "email.send", "message": {"to": ["x@y.com"], "subject": "S", "body": "B"}},
    )
    assert result == {"ok": False, "skipped": "openclaw owns this org"}
    assert calls == []


def test_finalize_time_asana_auto_push_is_suppressed(active_openclaw, monkeypatch):
    monkeypatch.setattr(settings, "asana_auto_execute", True)
    calls: list[dict] = []
    monkeypatch.setattr(
        executor,
        "_execute_approved",
        lambda org, aid, action: calls.append(action) or {"ok": True},
    )

    pushed = executor.auto_execute_asana(
        active_openclaw,
        [{
            "action_id": "asana1",
            "typed": {"type": "asana.create_task", "args": {"name": "Task"}},
        }],
    )
    assert pushed == 0
    assert calls == []


def test_pipedream_executor_is_suppressed_for_openclaw_org(active_openclaw, monkeypatch):
    monkeypatch.setattr(
        pipedream_executor,
        "_execute_approved",
        lambda *args, **kwargs: pytest.fail("legacy Pipedream executor wrote"),
    )

    result = pipedream_executor.execute_approved(
        active_openclaw,
        "a1",
        {"type": "asana.create_task", "task": {"name": "Task"}},
    )
    assert result == {"ok": False, "skipped": "openclaw owns this org"}


def test_cedric_action_requested_callback_is_suppressed(active_openclaw):
    session = SimpleNamespace(
        bot_id="bot_openclaw",
        org_id=active_openclaw,
        avatar_id="laura",
        integration={
            "org_id": active_openclaw,
            "callback_url": "https://cedric.example/events",
            "external_ref": {"team": "T1"},
        },
    )
    item = {
        "action_id": "oc_cb_1",
        "action": "Email the recap",
        "owner": "Laura",
        "due": "today",
    }

    canonical, created, outbox_id = outbox.persist_action_capture_once(session, item)
    assert canonical["action_id"] == "oc_cb_1"
    assert created is True
    assert outbox_id is None


def test_create_run_stays_queued_when_auto_run_is_off(active_openclaw, monkeypatch):
    monkeypatch.setattr(settings, "openclaw_auto_run", False)

    result = runtime.create_meeting_run("bot_openclaw_queued", _artifact(), active_openclaw)

    assert result["ok"] is True
    run = result["run"]
    assert run["status"] == "queued"
    assert run["actions"][0]["status"] == "queued"
    assert runtime.list_runs(active_openclaw)[0]["run_id"] == run["run_id"]


def test_auto_run_without_gateway_needs_attention_and_no_fallback(active_openclaw, monkeypatch):
    monkeypatch.setattr(settings, "openclaw_auto_run", True)
    monkeypatch.setattr(settings, "openclaw_gateway_url", "")
    calls: list[dict] = []
    monkeypatch.setattr(
        executor.native_runtime,
        "execute",
        lambda org, action: calls.append(action) or {"ok": True},
    )

    result = runtime.create_meeting_run("bot_openclaw_gateway", _artifact("oc_gateway"), active_openclaw)
    run_id = result["run"]["run_id"]

    assert _wait_until(
        lambda: (runtime.get_run(active_openclaw, run_id) or {}).get("status")
        == "needs_attention"
    )
    latest = ledger.action_statuses(["oc_gateway"], org_id=active_openclaw)["oc_gateway"]
    assert latest["status"] == "failed"
    assert "gateway" in latest["detail"].lower()
    assert calls == []


def test_tool_bridge_replays_duplicate_side_effect(active_openclaw, monkeypatch):
    monkeypatch.setattr(settings, "openclaw_auto_run", False)
    result = runtime.create_meeting_run("bot_openclaw_tool", _artifact("oc_tool"), active_openclaw)
    run_id = result["run"]["run_id"]
    token = runtime.mint_capability(active_openclaw, run_id)
    calls: list[dict] = []

    def fake_side_effect(org_id, action_id, tool_name, args):
        calls.append({"org_id": org_id, "action_id": action_id, "tool": tool_name, "args": args})
        return {"ok": True, "kind": "fake email", "ref": "msg_1", "route": "openclaw"}

    monkeypatch.setattr(runtime, "_execute_side_effect", fake_side_effect)
    body = {
        "action_id": "oc_tool",
        "step_id": "send-1",
        "args": {"to": ["a@b.com"], "subject": "S", "body": "B"},
    }

    first = runtime.run_tool(f"Bearer {token}", "gmail_send", body)
    second = runtime.run_tool(f"Bearer {token}", "gmail_send", body)

    assert first["ok"] is True and first["replay"] is False
    assert second["ok"] is True and second["replay"] is True
    assert len(calls) == 1


def test_dashboard_openclaw_runs_endpoint_reports_demo_org(active_openclaw):
    client = TestClient(main_module.app)

    response = client.get("/dashboard/openclaw/runs")

    assert response.status_code == 200
    body = response.json()
    assert body["org_id"] == active_openclaw
    assert body["active"] is True
    assert body["runs"] == []
