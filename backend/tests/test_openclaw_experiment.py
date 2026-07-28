from __future__ import annotations

import asyncio
import importlib
import json
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
from app.meeting import lifecycle
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

    def fake_side_effect(org_id, action_id, tool_name, action_type, args):
        calls.append(
            {
                "org_id": org_id,
                "action_id": action_id,
                "tool": tool_name,
                "action_type": action_type,
                "args": args,
            }
        )
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
    assert calls[0]["args"] == _artifact("oc_tool")["actions"][0]["typed"]["args"]


def test_tool_bridge_replays_action_with_a_different_step_id(
    active_openclaw, monkeypatch
):
    monkeypatch.setattr(settings, "openclaw_auto_run", False)
    result = runtime.create_meeting_run(
        "bot_openclaw_action_once",
        _artifact("oc_action_once"),
        active_openclaw,
    )
    token = runtime.mint_capability(
        active_openclaw, result["run"]["run_id"]
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        runtime,
        "_execute_side_effect",
        lambda *args: calls.append({"args": args})
        or {
            "ok": True,
            "kind": "fake email",
            "ref": "msg_once",
            "route": "openclaw",
        },
    )

    first = runtime.run_tool(
        token,
        "gmail_send",
        {"action_id": "oc_action_once", "step_id": "step-one"},
    )
    replay = runtime.run_tool(
        token,
        "gmail_send",
        {"action_id": "oc_action_once", "step_id": "step-two"},
    )

    assert first["ok"] is True
    assert replay["replay"] is True
    assert len(calls) == 1


def test_tool_bridge_rejects_foreign_action_and_wrong_tool(
    active_openclaw, monkeypatch
):
    monkeypatch.setattr(settings, "openclaw_auto_run", False)
    result = runtime.create_meeting_run(
        "bot_openclaw_scoped_tool",
        _artifact("oc_scoped"),
        active_openclaw,
    )
    token = runtime.mint_capability(
        active_openclaw, result["run"]["run_id"]
    )
    monkeypatch.setattr(
        runtime,
        "_execute_side_effect",
        lambda *_args: pytest.fail("invalid tool call reached a side effect"),
    )

    foreign = runtime.run_tool(
        token,
        "gmail_send",
        {"action_id": "not_in_this_run", "step_id": "step-one"},
    )
    wrong_tool = runtime.run_tool(
        token,
        "calendar_create_event",
        {"action_id": "oc_scoped", "step_id": "step-one"},
    )

    assert foreign["status"] == 403
    assert wrong_tool["status"] == 403


def test_pipedream_bridge_uses_openclaw_receipt_without_legacy_surface(
    active_openclaw, monkeypatch
):
    status_writes: list[dict] = []
    surface_calls: list[dict] = []
    monkeypatch.setattr(
        pipedream_executor.ledger,
        "set_action_status",
        lambda action_id, status, detail, **kwargs: status_writes.append(
            {
                "action_id": action_id,
                "status": status,
                "detail": detail,
                **kwargs,
            }
        )
        or True,
    )
    monkeypatch.setattr(
        main_module.cedric.callback,
        "send_action_event",
        lambda *_a, **_k: surface_calls.append({"called": True}),
    )

    result = pipedream_executor._settle(
        "oc_pd_action",
        active_openclaw,
        True,
        "asana.create_task",
        "https://app.asana.com/0/task",
        "",
        kind="Asana task",
        route="openclaw",
        mirror_surface=False,
    )

    assert result["ok"] is True
    assert result["route"] == "openclaw"
    assert result["runtime"] == "pipedream"
    assert status_writes[0]["receipt"]["route"] == "openclaw"
    assert status_writes[0]["receipt"]["runtime"] == "pipedream"
    assert status_writes[0]["detail"].startswith("OpenClaw via Pipedream")
    assert surface_calls == []


def test_dashboard_openclaw_runs_endpoint_reports_demo_org(active_openclaw):
    client = TestClient(main_module.app)

    response = client.get("/dashboard/openclaw/runs")

    assert response.status_code == 200
    body = response.json()
    assert body["org_id"] == active_openclaw
    assert body["active"] is True
    assert body["runs"] == []


def test_direct_meeting_runs_openclaw_once_end_to_end(
    active_openclaw, fresh_store, monkeypatch
):
    """Simulate a direct meeting, gateway, tool call, callback, and replay."""
    action_id = "oc_direct_meeting_email"
    gateway_requests: list[dict] = []
    vendor_calls: list[dict] = []
    legacy_surface_calls: list[dict] = []

    monkeypatch.setattr(settings, "openclaw_auto_run", True)
    monkeypatch.setattr(settings, "openclaw_gateway_url", "http://openclaw.test")
    monkeypatch.setattr(settings, "public_base_url", "http://laura.test")
    monkeypatch.setattr(settings, "scheduler_find_time", False)
    monkeypatch.setattr(settings, "asana_auto_execute", False)
    monkeypatch.setattr(runtime.control_plane, "enabled", lambda: False)

    monkeypatch.setattr(lifecycle.gemini_ears, "stop_session", lambda *_: None)
    monkeypatch.setattr(lifecycle.recall_client, "leave_call", lambda *_: None)
    monkeypatch.setattr(lifecycle.anam_client, "end_conversation", lambda *_: None)
    monkeypatch.setattr(lifecycle.gpu_runtime, "on_session_ended", lambda *_: None)
    monkeypatch.setattr(lifecycle.runpod_runtime, "on_session_ended", lambda *_: None)
    monkeypatch.setattr(lifecycle, "_avatar_asana_enabled", lambda *_: False)
    monkeypatch.setattr(lifecycle, "_avatar_pd_apps", lambda *_: [])
    monkeypatch.setattr(lifecycle, "prefill_summary_emails", lambda actions, *_: actions)
    monkeypatch.setattr(lifecycle, "type_actions", lambda actions, *_a, **_k: actions)
    monkeypatch.setattr(lifecycle, "headline_actions", lambda actions, *_: actions)
    monkeypatch.setattr(
        lifecycle.cedric,
        "deliver_ended",
        lambda *_a, **_k: pytest.fail("legacy Cedric finalizer ran"),
    )

    async def no_usage_close(*_args, **_kwargs):
        return None

    monkeypatch.setattr(lifecycle, "_close_usage_for", no_usage_close)
    monkeypatch.setattr(
        lifecycle,
        "post_meeting",
        lambda *_a, **_k: {
            "summary": "The customer requested an email recap.",
            "decisions": ["Proceed with the pilot."],
            "actions": [
                {
                    "action_id": action_id,
                    "item": "Email the customer the recap",
                    "owner": "Laura",
                    "assistant": True,
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
            "checklist": [],
            "missing_steps": [],
            "risks": [],
            "follow_up_email": {},
        },
    )

    class GatewayResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_gateway_post(url, *, json, headers, timeout):
        gateway_requests.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        if len(gateway_requests) == 1:
            return GatewayResponse(
                {
                    "id": "resp_simulated_1",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_email_1",
                            "name": "gmail_send",
                            "arguments": (
                                '{"action_id":"oc_direct_meeting_email",'
                                '"step_id":"send-recap-1"}'
                            ),
                        }
                    ],
                }
            )
        return GatewayResponse(
            {
                "id": "resp_simulated_2",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "The approved action completed.",
                            }
                        ],
                    }
                ],
            }
        )

    monkeypatch.setattr(main_module.httpx, "post", fake_gateway_post)
    monkeypatch.setattr(
        executor.native_runtime,
        "execute",
        lambda org, action: vendor_calls.append({"org": org, "action": action})
        or {"ok": True, "kind": "Gmail message", "ref": "msg_simulated_1"},
    )
    monkeypatch.setattr(
        main_module.cedric.callback,
        "send_action_event",
        lambda *_a, **_k: legacy_surface_calls.append({"called": True}),
    )

    bot_id = "bot_direct_openclaw"
    session = fresh_store.create(
        bot_id,
        "https://meet.google.com/open-claw-direct",
        "laura",
        org_id=active_openclaw,
    )
    session.memory_brief = ""
    session.add_utterance("Customer", "Laura, please send me the recap.")

    artifact = asyncio.run(
        lifecycle._finalize_session(bot_id, source="manual")
    )

    assert artifact is not None
    assert artifact["actions"][0]["execution_route"] == "openclaw"
    assert fresh_store.get(bot_id) is None
    assert _wait_until(lambda: len(gateway_requests) == 2)
    runs = runtime.list_runs(active_openclaw)
    assert len(runs) == 1
    run_id = runs[0]["run_id"]

    first_request = gateway_requests[0]
    second_request = gateway_requests[1]
    assert first_request["url"] == "http://openclaw.test/v1/responses"
    assert first_request["json"]["model"] == "openclaw"
    plan = json.loads(
        first_request["json"]["input"].split("\n", 1)[1]
    )
    assert plan["meeting_id"] == bot_id
    assert plan["raw_transcript_included"] is False
    assert "transcript" not in plan
    assert (
        "Customer: Please send the recap."
        not in first_request["json"]["input"]
    )
    gmail_tool = next(
        tool
        for tool in first_request["json"]["tools"]
        if tool["name"] == "gmail_send"
    )
    assert gmail_tool["parameters"]["properties"]["action_id"]["enum"] == [
        action_id
    ]
    assert (
        second_request["json"]["previous_response_id"]
        == "resp_simulated_1"
    )
    assert (
        second_request["json"]["input"][0]["type"]
        == "function_call_output"
    )

    legacy_result = executor.execute_approved(
        active_openclaw,
        action_id,
        {"type": "email.send", "message": {"to": ["wrong@example.com"]}},
    )
    assert legacy_result == {
        "ok": False,
        "skipped": "openclaw owns this org",
    }
    assert len(vendor_calls) == 1
    assert vendor_calls[0]["action"]["args"]["to"] == [
        "customer@example.com"
    ]
    assert legacy_surface_calls == []

    detail = runtime.run_detail(active_openclaw, run_id)
    assert detail is not None
    assert detail["status"] == "done"
    assert detail["actions"][0]["status"] == "done"
    canonical = ledger.action_statuses(
        [action_id], org_id=active_openclaw
    )[action_id]
    assert canonical["status"] == "done"
    assert canonical["detail"].startswith("OpenClaw via Laura native")

    replay = runtime.create_meeting_run(
        bot_id, artifact, active_openclaw
    )
    assert replay["replay"] is True
    assert replay["run"]["run_id"] == run_id
    assert len(vendor_calls) == 1
    assert len(runtime.list_runs(active_openclaw)) == 1
