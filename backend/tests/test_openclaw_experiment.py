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
from app.actions import action_plane, executor, ledger, outbox
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


def test_openclaw_gate_supports_explicit_all_orgs_wildcard(monkeypatch):
    monkeypatch.setattr(settings, "openclaw_experiment_enabled", True)
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", "*")

    assert gates.experiment_enabled_for_org("external-org-a") is True
    assert gates.experiment_enabled_for_org("external-org-b") is True
    assert gates.experiment_enabled_for_org("") is False
    assert gates.snapshot("external-org-a")["allowlisted"] is True


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


def test_auto_run_flag_cannot_bypass_explicit_approval(active_openclaw, monkeypatch):
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

    assert result["run"]["status"] == "queued"
    assert result["run"]["actions"][0]["status"] == "queued"
    assert ledger.action_statuses(
        ["oc_gateway"], org_id=active_openclaw
    )["oc_gateway"]["status"] == "proposed"
    assert calls == []

    approved = runtime.approve_action(active_openclaw, "oc_gateway")
    assert approved["ok"] is True
    assert _wait_until(
        lambda: (runtime.get_run(active_openclaw, run_id) or {}).get("status")
        == "needs_attention"
    )
    latest = ledger.action_statuses(["oc_gateway"], org_id=active_openclaw)["oc_gateway"]
    assert latest["status"] == "failed"
    assert "gateway" in latest["detail"].lower()
    assert calls == []


def test_repaired_action_refreshes_stale_run_before_retry(active_openclaw):
    action_id = "oc_stale_untyped_notion"
    artifact = _artifact(action_id)
    artifact["actions"][0]["typed"] = {"type": "", "args": {}}
    created = runtime.create_meeting_run(
        "bot_stale_untyped_notion", artifact, active_openclaw
    )
    run_id = created["run"]["run_id"]
    runtime._set_action_run(
        active_openclaw,
        run_id,
        action_id,
        "needs_attention",
        summary="OpenClaw execution did not complete",
        error="no_executable_tools",
    )
    runtime._update_run(
        active_openclaw,
        run_id,
        "needs_attention",
        error="OpenClaw found no executable canonical tools.",
    )
    ledger.set_action_status(
        action_id,
        "executing",
        "executing via openclaw",
        org_id=active_openclaw,
    )

    assert runtime.reconcile_failed_actions(active_openclaw, [action_id]) == 1
    assert ledger.action_statuses(
        [action_id], org_id=active_openclaw
    )[action_id]["status"] == "failed"

    repaired = {
        "type": "notion.create_page",
        "args": {"title": "Meeting work", "content": "Summary\n\n- [ ] Next"},
    }
    refreshed = runtime.refresh_action_spec_for_approval(
        active_openclaw, action_id, repaired
    )

    assert refreshed["updated"] is True
    assert refreshed["retry_ready"] is True
    run = runtime.get_run(active_openclaw, run_id)
    assert run is not None
    assert run["status"] == "queued"
    assert run["input"]["actions"][0]["typed"] == repaired
    assert runtime.run_detail(active_openclaw, run_id)["actions"][0]["status"] == "queued"

    assert ledger.reopen_failed_action(
        action_id, org_id=active_openclaw, detail="retrying repaired action"
    )
    approved = runtime.approve_action(
        active_openclaw,
        action_id,
        start=False,
        canonical_typed=repaired,
    )
    assert approved["ok"] is True
    assert runtime.run_detail(active_openclaw, run_id)["actions"][0]["status"] == "running"


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

    # run_tool enforces the approval boundary itself. Without approving first
    # this test would be asserting that an UNAPPROVED action reaches the
    # vendor — which is the hole the boundary now closes.
    assert runtime.approve_action(
        active_openclaw, "oc_tool", start=False
    )["ok"] is True

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

    # The approval boundary is enforced inside run_tool, so approve before
    # exercising the exactly-once behaviour this test is actually about.
    assert runtime.approve_action(
        active_openclaw, "oc_action_once", start=False
    )["ok"] is True

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


def test_approving_one_action_never_exposes_unapproved_siblings(
    active_openclaw, monkeypatch
):
    artifact = _artifact("oc_approved_only")
    artifact["actions"].append(
        {
            "action_id": "oc_still_waiting",
            "item": "Send a separate follow-up",
            "typed": {
                "type": "email.send",
                "args": {
                    "to": ["other@example.com"],
                    "subject": "Separate follow-up",
                    "body": "This action is not approved yet.",
                },
            },
        }
    )
    requests: list[dict] = []

    class GatewayResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_post(url, *, json, headers, timeout):
        requests.append(json)
        if len(requests) == 1:
            return GatewayResponse(
                {
                    "id": "resp_approved_only",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_approved_only",
                            "name": "gmail_send",
                            "arguments": {
                                "action_id": "oc_approved_only",
                                "step_id": "approved-only-1",
                            },
                        }
                    ],
                }
            )
        return GatewayResponse(
            {
                "id": "resp_approved_done",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "Completed."}
                        ],
                    }
                ],
            }
        )

    monkeypatch.setattr(settings, "openclaw_gateway_url", "http://openclaw.test")
    monkeypatch.setattr(main_module.httpx, "post", fake_post)
    monkeypatch.setattr(
        executor.native_runtime,
        "execute",
        lambda *_args: {
            "ok": True,
            "kind": "Gmail message",
            "ref": "msg_approved_only",
        },
    )
    created = runtime.create_meeting_run(
        "bot_openclaw_two_actions",
        artifact,
        active_openclaw,
    )

    approved = runtime.approve_action(active_openclaw, "oc_approved_only")

    assert approved["ok"] is True
    assert _wait_until(lambda: len(requests) == 2)
    tool = next(t for t in requests[0]["tools"] if t["name"] == "gmail_send")
    assert tool["parameters"]["properties"]["action_id"]["enum"] == [
        "oc_approved_only"
    ]
    detail = runtime.run_detail(active_openclaw, created["run"]["run_id"])
    assert detail is not None
    assert detail["status"] == "queued"
    statuses = {action["action_id"]: action["status"] for action in detail["actions"]}
    assert statuses == {
        "oc_approved_only": "done",
        "oc_still_waiting": "queued",
    }


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


def test_openclaw_chat_threads_are_separate_and_org_scoped(
    active_openclaw, monkeypatch
):
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", "*")
    first = runtime.create_chat_thread(active_openclaw)
    second = runtime.create_chat_thread(active_openclaw)
    assert first is not None and second is not None
    assert first["thread_id"] != second["thread_id"]

    runtime.add_chat_thread_message(
        active_openclaw, first["thread_id"], "user", "First conversation"
    )
    runtime.add_chat_thread_message(
        active_openclaw, second["thread_id"], "user", "Second conversation"
    )

    first_messages = runtime.list_chat_thread_messages(
        active_openclaw, first["thread_id"]
    )
    second_messages = runtime.list_chat_thread_messages(
        active_openclaw, second["thread_id"]
    )
    assert [message["text"] for message in first_messages] == [
        runtime.CHAT_GREETING,
        "First conversation",
    ]
    assert [message["text"] for message in second_messages] == [
        runtime.CHAT_GREETING,
        "Second conversation",
    ]
    assert runtime.get_chat_thread(
        "00000000-0000-0000-0000-000000000999", first["thread_id"]
    ) is None


def test_openclaw_chat_falls_back_when_postgres_tables_are_unavailable(
    active_openclaw, monkeypatch
):
    probes: list[str] = []

    class _Result:
        @staticmethod
        def scalar_one():
            return False

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _statement):
            probes.append("schema")
            return _Result()

    class _Engine:
        @staticmethod
        def connect():
            return _Connection()

    monkeypatch.setattr(runtime, "_CHAT_PG_SCHEMA_READY", None)
    monkeypatch.setattr(runtime, "_pg", lambda _org: True)
    monkeypatch.setattr(
        runtime.control_plane, "_get_engine", lambda: _Engine()
    )

    assert runtime._chat_pg(active_openclaw) is False
    assert runtime._chat_pg(active_openclaw) is False
    assert probes == ["schema"]


def test_dashboard_chat_endpoint_persists_thread_history(
    active_openclaw, monkeypatch
):
    client = TestClient(main_module.app)
    monkeypatch.setattr(
        runtime,
        "chat",
        lambda org, message, **kwargs: {
            "ok": True,
            "reply": f"Planned for {org}: {message}",
            "workflow": None,
        },
    )

    created = client.post(
        "/dashboard/openclaw/chats",
        json={"meeting_id": "bot_context"},
    )
    assert created.status_code == 200
    thread = created.json()["thread"]

    answered = client.post(
        "/dashboard/openclaw/chat",
        json={
            "thread_id": thread["thread_id"],
            "message": "Draft a Notion workflow",
            "meeting_id": "bot_context",
        },
    )
    assert answered.status_code == 200
    assert answered.json()["thread"]["title"] == "Draft a Notion workflow"

    detail = client.get(
        f"/dashboard/openclaw/chats/{thread['thread_id']}"
    ).json()["thread"]
    assert detail["meeting_id"] == "bot_context"
    assert [message["role"] for message in detail["messages"]] == [
        "assistant",
        "user",
        "assistant",
    ]
    assert detail["messages"][0]["text"] == runtime.CHAT_GREETING
    assert detail["messages"][-1]["text"].endswith(
        "Draft a Notion workflow"
    )


def test_openclaw_chat_answers_from_distilled_meeting_context_only(
    active_openclaw, monkeypatch
):
    runtime.create_meeting_run(
        "bot_openclaw_chat_context",
        _artifact("oc_chat_context"),
        active_openclaw,
    )
    requests: list[dict] = []

    class GatewayResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "id": "resp_chat_answer",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "reply": "The meeting decided to proceed with the pilot.",
                                        "workflow": None,
                                    }
                                ),
                            }
                        ],
                    }
                ],
            }

    def fake_post(url, *, json, headers, timeout):
        requests.append({"url": url, "json": json, "headers": headers})
        return GatewayResponse()

    monkeypatch.setattr(settings, "openclaw_gateway_url", "http://openclaw.test")
    monkeypatch.setattr(main_module.httpx, "post", fake_post)
    monkeypatch.setattr(
        runtime,
        "_connected_action_context",
        lambda _org: (
            ["gmail", "notion"],
            {
                "email.send": action_plane.params_schema({"type": "email.send"}),
                "notion.create_page": action_plane.params_schema(
                    {"type": "notion.create_page"}
                ),
                pipedream_executor.PROXY_ACTION_TYPE: action_plane.params_schema(
                    {"type": pipedream_executor.PROXY_ACTION_TYPE}
                ),
            },
        ),
    )

    result = runtime.chat(
        active_openclaw,
        "What did we decide and how does Laura work?",
        history=[
            {
                "role": "assistant",
                "text": "Here is the current workflow.",
                "workflow": {
                    "title": "Send recap",
                    "summary": "Draft to refine.",
                    "steps": [
                        {
                            "description": "Send the recap",
                            "action_type": "email.send",
                            "args": {
                                "to": ["customer@example.com"],
                                "subject": "Recap",
                                "body": "Thanks for meeting.",
                            },
                            "risk": "medium",
                            "depends_on": [],
                        }
                    ],
                },
            }
        ],
    )

    assert result["ok"] is True
    assert result["workflow"] is None
    payload = json.loads(requests[0]["json"]["input"])
    assert payload["meetings"][0]["summary"] == "Customer asked for a follow-up."
    assert payload["recent_chat"][0]["workflow"]["ready"] is True
    assert payload["recent_chat"][0]["workflow"]["steps"][0]["args"]["subject"] == "Recap"
    assert payload["raw_transcript_included"] is False
    assert "Customer: Please send the recap." not in requests[0]["json"]["input"]
    assert "validated deterministic actions" in payload["product"]["connections"]
    assert "additional API operations" in payload["product"]["execution_paths"]
    assert "not a runnable Laura/OpenClaw agent" in payload["product"]["agent_objects"]
    planner_tool_names = {
        tool["name"] for tool in requests[0]["json"]["tools"]
    }
    assert planner_tool_names == {"pipedream_proxy_read"}
    assert "Never output a \"pd.<app>.run\" action" in requests[0]["json"]["instructions"]
    assert "Never tell the user you can only perform preconfigured actions" in (
        requests[0]["json"]["instructions"]
    )
    assert "Never ask the user to paste API responses" in (
        requests[0]["json"]["instructions"]
    )
    assert "an omitted parent means a private workspace-root page" in (
        requests[0]["json"]["instructions"]
    )


def test_chat_discovers_every_healthy_pipedream_account(
    active_openclaw, monkeypatch
):
    monkeypatch.setattr(runtime.native_runtime, "catalog", lambda _org: [])
    monkeypatch.setattr(settings, "pipedream_executor", True)
    monkeypatch.setattr(runtime.pipedream_client, "enabled", lambda: True)
    monkeypatch.setattr(
        runtime.pipedream_client,
        "list_accounts",
        lambda _org: [
            {"app": "notion", "healthy": True},
            {"app": "linear", "healthy": True},
            {"app": "old_crm", "healthy": False},
        ],
    )

    apps, schemas = runtime._connected_action_context(active_openclaw)

    assert apps == ["linear", "notion"]
    assert list(schemas) == ["notion.create_page", "pipedream.proxy_request"]
    assert [
        field["name"] for field in schemas["notion.create_page"]
    ] == ["parent", "title", "content"]
    assert [
        field["name"] for field in schemas["pipedream.proxy_request"]
    ] == ["app", "method", "url", "body", "headers"]


def test_chat_accepts_safe_connected_app_proxy_workflow(
    active_openclaw, monkeypatch
):
    proxy_schema = action_plane.params_schema(
        {"type": pipedream_executor.PROXY_ACTION_TYPE}
    )
    monkeypatch.setattr(
        runtime,
        "_connected_action_context",
        lambda _org: (
            ["notion"],
            {pipedream_executor.PROXY_ACTION_TYPE: proxy_schema},
        ),
    )
    workflow = {
        "title": "Update the Notion page",
        "steps": [
            {
                "description": "Archive the approved Notion page",
                "action_type": pipedream_executor.PROXY_ACTION_TYPE,
                "args": {
                    "app": "notion",
                    "method": "PATCH",
                    "url": "https://api.notion.com/v1/pages/page-1",
                    "body": {"archived": True},
                },
            }
        ],
    }

    normalized = runtime._normalize_chat_workflow(
        active_openclaw, workflow
    )

    assert normalized is not None
    assert normalized["ready"] is True
    assert normalized["steps"][0]["risk"] == "high"

    workflow["steps"][0]["args"]["url"] = "https://example.test/steal"
    blocked = runtime._normalize_chat_workflow(active_openclaw, workflow)
    assert blocked is not None
    assert blocked["ready"] is False
    assert "valid_proxy_request" in blocked["steps"][0]["missing_params"]


def test_chat_accepts_complex_notion_proxy_and_deterministic_workflow(
    active_openclaw, monkeypatch
):
    monkeypatch.setattr(
        runtime,
        "_connected_action_context",
        lambda _org: (
            ["notion"],
            {
                "notion.create_page": action_plane.params_schema(
                    {"type": "notion.create_page"}
                ),
                pipedream_executor.PROXY_ACTION_TYPE: action_plane.params_schema(
                    {"type": pipedream_executor.PROXY_ACTION_TYPE}
                ),
            },
        ),
    )
    page_id = "3ab01a33-6260-815f-b72b-c8d24473a51b"
    workflow = {
        "title": "Update the QA page and create a follow-up",
        "steps": [
            {
                "description": "Mark the existing QA page with a rocket icon",
                "action_type": pipedream_executor.PROXY_ACTION_TYPE,
                "args": {
                    "app": "notion",
                    "method": "PATCH",
                    "url": f"https://api.notion.com/v1/pages/{page_id}",
                    "body": {"icon": {"type": "emoji", "emoji": "\U0001f680"}},
                    "headers": {"Notion-Version": "2022-06-28"},
                },
            },
            {
                "description": "Append the workflow verification note",
                "action_type": pipedream_executor.PROXY_ACTION_TYPE,
                "args": {
                    "app": "notion",
                    "method": "PATCH",
                    "url": f"https://api.notion.com/v1/blocks/{page_id}/children",
                    "body": {
                        "children": [
                            {
                                "object": "block",
                                "type": "paragraph",
                                "paragraph": {
                                    "rich_text": [
                                        {
                                            "type": "text",
                                            "text": {
                                                "content": "Complex workflow verified."
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                    "headers": {"Notion-Version": "2022-06-28"},
                },
                "depends_on": [1],
            },
            {
                "description": "Create the private follow-up page",
                "action_type": "notion.create_page",
                "args": {
                    "title": "OpenClaw complex workflow QA",
                    "content": "The connected-app workflow completed.",
                },
                "depends_on": [1, 2],
            },
        ],
    }

    normalized = runtime._normalize_chat_workflow(active_openclaw, workflow)

    assert normalized is not None
    assert normalized["ready"] is True
    assert [step["action_type"] for step in normalized["steps"]] == [
        pipedream_executor.PROXY_ACTION_TYPE,
        pipedream_executor.PROXY_ACTION_TYPE,
        "notion.create_page",
    ]
    assert [step["risk"] for step in normalized["steps"]] == [
        "high",
        "high",
        "low",
    ]
    assert [step["depends_on"] for step in normalized["steps"]] == [
        [],
        [1],
        [1, 2],
    ]
    assert normalized["steps"][2]["missing_params"] == []


def test_chat_accepts_prebuilt_actions_only_for_connected_pipedream_apps(
    active_openclaw, monkeypatch
):
    monkeypatch.setattr(
        runtime,
        "_connected_action_context",
        lambda _org: (["notion"], {}),
    )
    monkeypatch.setattr(
        runtime.pipedream_client,
        "get_component",
        lambda key: {
            "key": key,
            "name": "Create page",
            "configurable_props": [
                {"name": "notion", "type": "app"},
                {"name": "title", "type": "string", "optional": False},
            ],
        },
    )
    workflow = {
        "title": "Update the workspace",
        "steps": [
            {
                "description": "Create a Notion page",
                "action_type": "pd.notion.run",
                "args": {
                    "action_key": "notion-create-page",
                    "props": {
                        "title": "Meeting recap",
                        "notion": {"authProvisionId": "must-be-dropped"},
                        "unknown": "must-be-dropped",
                    },
                },
            },
            {
                "description": "Write to an unconnected app",
                "action_type": "pd.salesforce.run",
                "args": {
                    "action_key": "salesforce-create-lead",
                    "props": {},
                },
            },
        ],
    }

    normalized = runtime._normalize_chat_workflow(active_openclaw, workflow)

    assert normalized is not None
    assert normalized["ready"] is True
    assert [step["action_type"] for step in normalized["steps"]] == [
        "pd.notion.run"
    ]
    assert normalized["steps"][0]["args"]["props"] == {
        "title": "Meeting recap"
    }


def test_chat_workflow_starts_only_after_explicit_user_action(
    active_openclaw, monkeypatch
):
    starts: list[tuple[str, str]] = []
    schema = action_plane.params_schema({"type": "email.send"})
    monkeypatch.setattr(
        runtime,
        "_connected_action_context",
        lambda _org: (["gmail"], {"email.send": schema}),
    )
    monkeypatch.setattr(
        runtime,
        "start_run_async",
        lambda org, run_id: starts.append((org, run_id)),
    )
    workflow = {
        "title": "Send the approved recap",
        "summary": "One explicit email step.",
        "steps": [
            {
                "description": "Send the recap email",
                "action_type": "email.send",
                "args": {
                    "to": ["customer@example.com"],
                    "subject": "Recap",
                    "body": "Thanks for meeting.",
                },
                "risk": "medium",
                "depends_on": [],
            }
        ],
    }

    normalized = runtime._normalize_chat_workflow(active_openclaw, workflow)
    assert normalized is not None and normalized["ready"] is True
    assert starts == []

    result = runtime.start_chat_workflow(
        active_openclaw,
        normalized,
        laura_user_id="test-user",
    )

    assert result["ok"] is True
    assert starts == [(active_openclaw, result["run_id"])]
    detail = runtime.run_detail(active_openclaw, result["run_id"])
    assert detail is not None
    assert detail["actions"][0]["status"] == "running"
    action_id = detail["actions"][0]["action_id"]
    canonical = ledger.action_statuses(
        [action_id], org_id=active_openclaw
    )[action_id]
    assert canonical["status"] == "executing"
    replay = runtime.approve_action(
        active_openclaw,
        action_id,
        decided_via="dashboard",
        laura_user_id="test-user",
        record_decision=True,
    )
    assert replay["ok"] is True
    assert replay["replay"] is True
    assert replay["status"] == "running"
    assert starts == [(active_openclaw, result["run_id"])]
    assert ledger.action_statuses(
        [action_id], org_id=active_openclaw
    )[action_id]["status"] == "executing"
    assert (
        ledger.get_action_decision(action_id, org_id=active_openclaw)[
            "decided_via"
        ]
        == "openclaw_chat"
    )
    workflow_id = result["workflow_id"]
    artifact = store.get_artifact(workflow_id, org_id=active_openclaw)
    assert artifact is not None
    assert artifact["source_surface"] == "openclaw_chat"
    assert artifact["actions"][0]["workflow_id"] == workflow_id
    monkeypatch.setattr(
        runtime, "_connected_action_context", lambda _org: ([], {})
    )
    context = runtime.saved_chat_workflow_context(
        active_openclaw, workflow_id
    )
    assert context is not None
    assert context["workflow"]["title"] == "Send the approved recap"
    assert context["workflow"]["steps"][0]["action_type"] == "email.send"
    assert runtime.saved_chat_workflow_context(
        "00000000-0000-0000-0000-000000000999", workflow_id
    ) is None
    refinement = runtime.create_chat_thread(
        active_openclaw, seed_workflow=context["workflow"]
    )
    assert refinement is not None
    seeded = runtime.list_chat_thread_messages(
        active_openclaw, refinement["thread_id"]
    )
    assert seeded[0]["workflow"]["title"] == "Send the approved recap"
    assert "from Action Center" in seeded[0]["text"]
    summary = TestClient(main_module.app).get("/dashboard/summary").json()
    workflow_meeting = next(
        meeting
        for meeting in summary["meetings"]
        if meeting["bot_id"] == workflow_id
    )
    assert workflow_meeting["source_surface"] == "openclaw_chat"
    card = workflow_meeting["actions"][0]
    assert card["workflow_id"] == workflow_id
    assert card["workflow_title"] == "Send the approved recap"
    assert card["workflow_step_count"] == 1


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
    assert gateway_requests == []
    assert vendor_calls == []

    approved = runtime.approve_action(active_openclaw, action_id)
    assert approved["ok"] is True
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
