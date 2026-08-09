"""Server-side dependency enforcement for OpenClaw runs.

Scope and method, because both are deliberate:

* **Fakes only.** No gateway, no vendor, no network, no keys. The OpenResponses
  endpoint is emulated by patching ``httpx.post`` (``run_openclaw`` does a
  function-local ``import httpx``, so the module attribute is the seam), and
  every vendor write goes through ``runtime._execute_side_effect``, which is
  replaced by a recorder. A test that reaches a real vendor is a bug in the
  test.
* **No ``app.main``.** These tests import ``app.openclaw.runtime`` and its
  narrow dependencies. The one HTTP test builds the smallest possible FastAPI
  app around ``app.openclaw.router`` instead of the full application, so the
  focused suite never pays for (or waits on) the whole app graph.
* **The contract under test:** an action runs only after every id in its
  ``depends_on`` is ``done``; a dependent whose prerequisite can never finish
  is settled without a vendor call; a structurally impossible plan is refused
  fail-closed and never reaches the gateway; and none of it depends on the
  order in which the gateway happens to call.
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store
from app.actions import ledger, outbox
from app.config import settings
from app.openclaw import dependencies, runtime

ORG_B = "00000000-0000-4000-8000-0000000000b0"


# ---------------------------------------------------------------- fixtures


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
    """One allowlisted org, key-free, forced onto the SQLite path."""
    org = settings.demo_org_id
    monkeypatch.setattr(settings, "openclaw_experiment_enabled", True)
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", f"{org},{ORG_B}")
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(runtime.control_plane, "enabled", lambda: False)
    return org


# ----------------------------------------------------------------- helpers


def _step(action_id: str, item: str, typed: dict, depends_on: list[str]) -> dict:
    return {
        "action_id": action_id,
        "item": item,
        "owner": "Laura",
        "depends_on": list(depends_on),
        "typed": typed,
    }


def _notion_actions() -> list[dict]:
    """update page -> append blocks -> create follow-up page, strictly chained."""
    return [
        _step(
            "oc_notion_1",
            "Update the project page status",
            {
                "type": "pipedream.proxy_request",
                "args": {
                    "app": "notion",
                    "method": "PATCH",
                    "url": "https://api.notion.com/v1/pages/PAGE_1",
                    "body": {"properties": {"Status": "Shipped"}},
                },
            },
            [],
        ),
        _step(
            "oc_notion_2",
            "Append the decision blocks to that page",
            {
                "type": "pipedream.proxy_request",
                "args": {
                    "app": "notion",
                    "method": "PATCH",
                    "url": "https://api.notion.com/v1/blocks/PAGE_1/children",
                    "body": {"children": [{"type": "paragraph"}]},
                },
            },
            ["oc_notion_1"],
        ),
        _step(
            "oc_notion_3",
            "Create the follow-up page",
            {
                "type": "notion.create_page",
                "args": {"parent": "PAGE_1", "title": "Follow-up"},
            },
            ["oc_notion_2"],
        ),
    ]


def _artifact(actions: list[dict] | None = None, org: str = "") -> dict:
    """A finalized-meeting artifact. Carries a transcript ON PURPOSE: the
    no-leak test proves the run payload never copies it."""
    return {
        "summary": "We agreed to update the Notion page and open a follow-up.",
        "decisions": ["Ship the pilot."],
        "actions": actions if actions is not None else _notion_actions(),
        "avatar_id": "laura",
        "org_id": org or settings.demo_org_id,
        "meeting_url": "https://meet.google.com/openclaw-deps",
        "transcript": "Customer: PLEASE-NEVER-LEAVE-LAURA. Laura: understood.",
    }


def _make_run(org: str, meeting_id: str, actions: list[dict] | None = None) -> str:
    created = runtime.create_meeting_run(meeting_id, _artifact(actions, org), org)
    assert created["ok"] is True, created
    return str(created["run"]["run_id"])


def _approve_all(org: str, action_ids: list[str]) -> None:
    for aid in action_ids:
        result = runtime.approve_action(org, aid, start=False)
        assert result["ok"] is True, (aid, result)


def _statuses(org: str, run_id: str) -> dict[str, str]:
    detail = runtime.run_detail(org, run_id) or {}
    return {a["action_id"]: a["status"] for a in detail.get("actions") or []}


def _record_side_effect(monkeypatch, *, fail: set[str] | None = None) -> list[dict]:
    """Replace the only door to a vendor with a recorder."""
    failing = fail or set()
    calls: list[dict] = []

    def fake(org_id, action_id, tool_name, action_type, args):
        calls.append(
            {
                "org_id": org_id,
                "action_id": action_id,
                "tool": tool_name,
                "type": action_type,
                "args": args,
            }
        )
        if action_id in failing:
            return {"ok": False, "error": "vendor rejected the request"}
        return {
            "ok": True,
            "kind": "Notion write",
            "ref": f"ref_{action_id}",
            "route": "openclaw",
        }

    monkeypatch.setattr(runtime, "_execute_side_effect", fake)
    return calls


class _GatewayResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, body: dict):
        self._body = body

    def json(self) -> dict:
        return self._body


def _fake_gateway(monkeypatch, call_plan: list[list[str]]) -> list[dict]:
    """Emulate OpenResponses: one turn per entry, each naming action ids to call.

    An empty final turn (no ids) is appended automatically — that is how the
    real API signals "no more tool calls", which is what drives the run to its
    terminal status.
    """
    monkeypatch.setattr(settings, "openclaw_gateway_url", "http://openclaw.test")
    requests: list[dict] = []
    turns = [*call_plan, []]

    # ``post``'s ``json`` keyword shadows the module inside its body, so the
    # dumper is bound here where the name still means the module.
    json_dumps = json.dumps

    def post(url, *, json, headers, timeout):  # noqa: A002 - vendor signature
        index = len(requests)
        requests.append({"url": url, "json": json, "headers": headers})
        turn = turns[index] if index < len(turns) else []
        if not turn:
            return _GatewayResponse(
                {
                    "id": f"resp_{index + 1}",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "done"}],
                        }
                    ],
                }
            )
        return _GatewayResponse(
            {
                "id": f"resp_{index + 1}",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": f"call_{index + 1}_{n}",
                        "name": "pipedream_run_app_action",
                        "arguments": json_dumps(
                            {"action_id": aid, "step_id": f"step_{aid}"}
                        ),
                    }
                    for n, aid in enumerate(turn)
                ],
            }
        )

    monkeypatch.setattr(httpx, "post", post)
    return requests


# ------------------------------------------------- 1. pure dependency rules


def test_dependency_graph_rejects_unknown_self_forward_and_cycles():
    unknown = dependencies.validate_dependency_graph(
        [{"action_id": "a"}, {"action_id": "b", "depends_on": ["ghost"]}]
    )
    assert "ghost" in unknown["b"] and "a" not in unknown

    itself = dependencies.validate_dependency_graph(
        [{"action_id": "a", "depends_on": ["a"]}]
    )
    assert itself["a"] == "action depends on itself"

    forward = dependencies.validate_dependency_graph(
        [{"action_id": "a", "depends_on": ["b"]}, {"action_id": "b"}]
    )
    assert "declared after this action" in forward["a"]
    assert "b" not in forward

    cycle = dependencies.validate_dependency_graph(
        [
            {"action_id": "a", "depends_on": ["c"]},
            {"action_id": "b", "depends_on": ["a"]},
            {"action_id": "c", "depends_on": ["b"]},
        ]
    )
    assert set(cycle) == {"a", "b", "c"}
    assert all("circular dependency" in reason for reason in cycle.values())

    duplicates = dependencies.validate_dependency_graph(
        [{"action_id": "a"}, {"action_id": "a"}]
    )
    assert "duplicate" in duplicates["a"]

    # A clean chain is accepted, and only a clean chain.
    assert dependencies.validate_dependency_graph(_notion_actions()) == {}


def test_dependency_rejection_propagates_to_everything_downstream():
    problems = dependencies.validate_dependency_graph(
        [
            {"action_id": "a", "depends_on": ["ghost"]},
            {"action_id": "b", "depends_on": ["a"]},
            {"action_id": "c", "depends_on": ["b"]},
            {"action_id": "d"},
        ]
    )
    assert set(problems) == {"a", "b", "c"}
    assert "rejected action" in problems["c"]


def test_gate_allows_only_when_every_prerequisite_is_done():
    action = {"action_id": "b", "depends_on": ["a1", "a2"]}
    assert dependencies.gate(action, {"a1": "done", "a2": "done"})[0] == "allow"
    assert dependencies.gate(action, {"a1": "done", "a2": "running"})[0] == "wait"
    assert dependencies.gate(action, {"a1": "done", "a2": "queued"})[0] == "wait"
    for blocking in ("failed", "cancelled", "needs_attention"):
        decision, reason, blockers = dependencies.gate(
            action, {"a1": "done", "a2": blocking}
        )
        assert decision == "blocked"
        assert blockers == ["a2"] and blocking in reason
    # An unknown prerequisite state is never "satisfied" — fail closed.
    assert dependencies.gate(action, {})[0] == "wait"
    # No declared prerequisite: never gated.
    assert dependencies.gate({"action_id": "solo"}, {})[0] == "allow"


def test_blocked_status_is_not_read_as_success_by_the_run_summary():
    """Regression guard: settling a blocked dependent 'cancelled' would make
    ``_finish_openresponses_run`` report a broken chain as a completed run."""
    assert dependencies.BLOCKED_STATUS not in ("done", "cancelled")
    assert dependencies.BLOCKED_STATUS in runtime.TERMINAL_RUN_STATUSES
    assert dependencies.BLOCKED_STATUS in dependencies.BLOCKING_STATUSES


# ---------------------------------------- 2. happy path: three chained steps


def test_three_step_notion_workflow_executes_in_dependency_order(
    active_openclaw, monkeypatch
):
    org = active_openclaw
    run_id = _make_run(org, "bot_notion_ok")
    ids = ["oc_notion_1", "oc_notion_2", "oc_notion_3"]
    calls = _record_side_effect(monkeypatch)
    requests = _fake_gateway(monkeypatch, [[ids[0]], [ids[1]], [ids[2]]])
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)

    _approve_all(org, ids)
    runtime.run_openclaw(org, run_id)

    assert [c["action_id"] for c in calls] == ids
    assert _statuses(org, run_id) == {i: "done" for i in ids}
    detail = runtime.run_detail(org, run_id)
    assert detail["status"] == "done"
    assert detail["actions"][2]["receipt"]["ref"] == "ref_oc_notion_3"
    # The canonical args executed are the stored ones, never the gateway's.
    assert calls[0]["args"]["url"] == "https://api.notion.com/v1/pages/PAGE_1"
    assert calls[2]["type"] == "notion.create_page"
    # 3 tool turns + the closing turn.
    assert len(requests) == 4


def test_no_vendor_write_happens_before_the_explicit_start(
    active_openclaw, monkeypatch
):
    org = active_openclaw
    calls = _record_side_effect(monkeypatch)
    requests = _fake_gateway(monkeypatch, [["oc_notion_1"]])
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)

    run_id = _make_run(org, "bot_notion_unapproved")

    # Creating the run is not approval: nothing has been offered or executed.
    assert calls == [] and requests == []
    assert set(_statuses(org, run_id).values()) == {"queued"}
    assert runtime.get_run(org, run_id)["status"] == "queued"

    # And a run with nothing approved never reaches the gateway either.
    runtime.run_openclaw(org, run_id)
    assert requests == [] and calls == []
    assert runtime.get_run(org, run_id)["status"] == "queued"


def test_unapproved_siblings_are_never_exposed_to_the_gateway(
    active_openclaw, monkeypatch
):
    org = active_openclaw
    run_id = _make_run(org, "bot_notion_partial")
    _record_side_effect(monkeypatch)
    requests = _fake_gateway(monkeypatch, [["oc_notion_1"]])
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)

    _approve_all(org, ["oc_notion_1"])
    runtime.run_openclaw(org, run_id)

    enums = [
        tool["parameters"]["properties"]["action_id"]["enum"]
        for tool in requests[0]["json"]["tools"]
    ]
    assert enums == [["oc_notion_1"]]
    plan_ids = [
        a["action_id"]
        for a in json.loads(requests[0]["json"]["input"].split("\n", 1)[1])["actions"]
    ]
    assert plan_ids == ["oc_notion_1"]


# ------------------------------------------------- 3. refusals and blocking


def test_out_of_order_gateway_call_is_refused_without_a_vendor_write(
    active_openclaw, monkeypatch
):
    org = active_openclaw
    run_id = _make_run(org, "bot_notion_out_of_order")
    ids = ["oc_notion_1", "oc_notion_2", "oc_notion_3"]
    calls = _record_side_effect(monkeypatch)
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)
    _approve_all(org, ids)

    token = runtime.mint_capability(org, run_id)
    early = runtime.run_tool(
        token,
        "pipedream_run_app_action",
        {"action_id": "oc_notion_3", "step_id": "s_early"},
    )

    assert early["ok"] is False
    assert early["code"] == runtime.DEPENDENCY_NOT_READY
    assert early["status"] == 409
    assert early["blocked_by"] == ["oc_notion_2"]
    assert calls == []
    # Refused, not settled: the step stays claimed and runnable.
    assert _statuses(org, run_id)["oc_notion_3"] == "running"
    assert runtime._tool_existing_for_action(org, run_id, "oc_notion_3") is None

    # And it succeeds once its chain really is done.
    for aid in ids:
        result = runtime.run_tool(
            token,
            "pipedream_run_app_action",
            {"action_id": aid, "step_id": f"s_{aid}"},
        )
        assert result["ok"] is True, (aid, result)
    assert [c["action_id"] for c in calls] == ids


def test_first_step_failure_keeps_steps_two_and_three_away_from_the_vendor(
    active_openclaw, monkeypatch
):
    org = active_openclaw
    run_id = _make_run(org, "bot_notion_fail")
    ids = ["oc_notion_1", "oc_notion_2", "oc_notion_3"]
    calls = _record_side_effect(monkeypatch, fail={"oc_notion_1"})
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)
    _approve_all(org, ids)

    token = runtime.mint_capability(org, run_id)
    first = runtime.run_tool(
        token,
        "pipedream_run_app_action",
        {"action_id": "oc_notion_1", "step_id": "s1"},
    )
    assert first["ok"] is False and first["status"] == "failed"

    # The tail was settled eagerly — no second gateway call was needed.
    statuses = _statuses(org, run_id)
    assert statuses == {
        "oc_notion_1": "failed",
        "oc_notion_2": "needs_attention",
        "oc_notion_3": "needs_attention",
    }
    detail = runtime.run_detail(org, run_id)
    blocked = {a["action_id"]: a for a in detail["actions"]}["oc_notion_2"]
    assert blocked["receipt"]["executed"] is False
    assert blocked["receipt"]["blocked_by"] == ["oc_notion_1"]
    assert "oc_notion_1" in blocked["error"]

    # And a late gateway call for a blocked step still never reaches a vendor.
    late = runtime.run_tool(
        token,
        "pipedream_run_app_action",
        {"action_id": "oc_notion_2", "step_id": "s2"},
    )
    assert late["ok"] is False
    assert late["code"] == runtime.DEPENDENCY_BLOCKED
    assert late["status"] == 409
    assert [c["action_id"] for c in calls] == ["oc_notion_1"]


def test_run_reaches_a_truthful_terminal_status_after_a_blocked_chain(
    active_openclaw, monkeypatch
):
    org = active_openclaw
    run_id = _make_run(org, "bot_notion_terminal")
    ids = ["oc_notion_1", "oc_notion_2", "oc_notion_3"]
    _record_side_effect(monkeypatch, fail={"oc_notion_1"})
    _fake_gateway(monkeypatch, [[ids[0]]])
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)

    _approve_all(org, ids)
    runtime.run_openclaw(org, run_id)

    detail = runtime.run_detail(org, run_id)
    assert detail["status"] == "needs_attention"
    assert {a["action_id"]: a["status"] for a in detail["actions"]} == {
        "oc_notion_1": "failed",
        "oc_notion_2": "needs_attention",
        "oc_notion_3": "needs_attention",
    }


def test_a_cyclic_plan_is_refused_before_the_gateway_is_ever_called(
    active_openclaw, monkeypatch
):
    org = active_openclaw
    steps = _notion_actions()
    steps[0]["depends_on"] = ["oc_notion_3"]  # 1 -> 3 -> 2 -> 1
    run_id = _make_run(org, "bot_notion_cycle", steps)
    calls = _record_side_effect(monkeypatch)
    requests = _fake_gateway(monkeypatch, [["oc_notion_1"]])
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)

    _approve_all(org, ["oc_notion_1", "oc_notion_2", "oc_notion_3"])
    runtime.run_openclaw(org, run_id)

    assert requests == []
    assert calls == []
    detail = runtime.run_detail(org, run_id)
    assert detail["status"] == "needs_attention"
    assert set(_statuses(org, run_id).values()) == {"needs_attention"}
    first = detail["actions"][0]
    assert first["receipt"]["error"] == runtime.DEPENDENCY_PLAN_REJECTED
    assert "circular dependency" in first["error"]


def test_an_unknown_dependency_is_refused_at_the_tool_bridge(
    active_openclaw, monkeypatch
):
    org = active_openclaw
    steps = _notion_actions()
    steps[1]["depends_on"] = ["oc_not_in_this_run"]
    run_id = _make_run(org, "bot_notion_unknown", steps)
    calls = _record_side_effect(monkeypatch)
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)
    _approve_all(org, ["oc_notion_1", "oc_notion_2", "oc_notion_3"])

    token = runtime.mint_capability(org, run_id)
    refused = runtime.run_tool(
        token,
        "pipedream_run_app_action",
        {"action_id": "oc_notion_2", "step_id": "s2"},
    )

    assert refused["ok"] is False
    assert refused["code"] == runtime.DEPENDENCY_PLAN_REJECTED
    assert "oc_not_in_this_run" in refused["error"]
    assert calls == []
    assert _statuses(org, run_id)["oc_notion_2"] == "needs_attention"


# ----------------------------------------------------- 4. exactly-once


def test_replay_with_a_different_step_id_never_writes_twice(
    active_openclaw, monkeypatch
):
    org = active_openclaw
    run_id = _make_run(org, "bot_notion_replay")
    ids = ["oc_notion_1", "oc_notion_2"]
    calls = _record_side_effect(monkeypatch)
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)
    _approve_all(org, ids)
    token = runtime.mint_capability(org, run_id)

    runtime.run_tool(
        token, "pipedream_run_app_action",
        {"action_id": "oc_notion_1", "step_id": "step_a"},
    )
    first = runtime.run_tool(
        token, "pipedream_run_app_action",
        {"action_id": "oc_notion_2", "step_id": "step_b"},
    )
    same = runtime.run_tool(
        token, "pipedream_run_app_action",
        {"action_id": "oc_notion_2", "step_id": "step_b"},
    )
    different = runtime.run_tool(
        token, "pipedream_run_app_action",
        {"action_id": "oc_notion_2", "step_id": "TOTALLY-DIFFERENT"},
    )

    assert first["ok"] is True and first["replay"] is False
    assert same["replay"] is True and same["ok"] is True
    assert different["replay"] is True and different["ok"] is True
    assert [c["action_id"] for c in calls] == ids


def test_concurrent_calls_for_one_action_produce_exactly_one_write(
    active_openclaw, monkeypatch
):
    org = active_openclaw
    run_id = _make_run(org, "bot_notion_race")
    calls = _record_side_effect(monkeypatch)
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)
    _approve_all(org, ["oc_notion_1"])
    token = runtime.mint_capability(org, run_id)

    barrier = threading.Barrier(4)
    results: list[dict] = []
    lock = threading.Lock()

    def attempt(n: int) -> None:
        barrier.wait()
        out = runtime.run_tool(
            token,
            "pipedream_run_app_action",
            {"action_id": "oc_notion_1", "step_id": f"racer_{n}"},
        )
        with lock:
            results.append(out)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 4
    assert len(calls) == 1
    assert _statuses(org, run_id)["oc_notion_1"] == "done"


# -------------------------------------------------------- 5. tenancy & PII


def test_a_capability_cannot_reach_another_organizations_run(
    active_openclaw, monkeypatch
):
    org = active_openclaw
    run_id = _make_run(org, "bot_notion_org_a")
    calls = _record_side_effect(monkeypatch)
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)
    _approve_all(org, ["oc_notion_1"])

    # Layer 1 — a capability naming org B and org A's run does not even
    # verify: the run lookup inside verify_capability is org-scoped, so the
    # token is rejected before any action is resolved.
    forged = runtime.run_tool(
        runtime.mint_capability(ORG_B, run_id),
        "pipedream_run_app_action",
        {"action_id": "oc_notion_1", "step_id": "s1"},
    )
    assert forged["ok"] is False and forged["status"] == 401

    # Layer 2 — org B holds a perfectly VALID capability for its own run and
    # asks for org A's action id. The action is not in that run: refused.
    other_run = _make_run(
        ORG_B,
        "bot_notion_org_b",
        [
            _step(
                "oc_org_b_only",
                "Org B's own unrelated page",
                {
                    "type": "notion.create_page",
                    "args": {"parent": "PAGE_B", "title": "B"},
                },
                [],
            )
        ],
    )
    cross = runtime.run_tool(
        runtime.mint_capability(ORG_B, other_run),
        "pipedream_run_app_action",
        {"action_id": "oc_notion_1", "step_id": "s1"},
    )
    assert cross["ok"] is False and cross["status"] == 403
    assert calls == []

    # Org B genuinely cannot see org A's run at all.
    assert runtime.get_run(ORG_B, run_id) is None
    assert runtime.run_detail(ORG_B, run_id) is None
    assert [r["run_id"] for r in runtime.list_runs(ORG_B)] == [other_run]
    assert [r["run_id"] for r in runtime.list_runs(org)] == [run_id]
    assert _statuses(org, run_id)["oc_notion_1"] == "running"


def test_no_raw_transcript_is_ever_sent_to_openclaw(active_openclaw, monkeypatch):
    org = active_openclaw
    run_id = _make_run(org, "bot_notion_pii")
    _record_side_effect(monkeypatch)
    requests = _fake_gateway(monkeypatch, [["oc_notion_1"]])
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)

    _approve_all(org, ["oc_notion_1"])
    runtime.run_openclaw(org, run_id)

    body = json.dumps(requests[0]["json"])
    assert "PLEASE-NEVER-LEAVE-LAURA" not in body
    assert "transcript" not in json.loads(
        requests[0]["json"]["input"].split("\n", 1)[1]
    )
    plan = json.loads(requests[0]["json"]["input"].split("\n", 1)[1])
    assert plan["raw_transcript_included"] is False
    stored = runtime.get_run(org, run_id)["input"]
    assert "transcript" not in stored


# ------------------------------------------------------ 6. browser fallback


def test_manual_browser_fallback_stays_needs_attention(active_openclaw, monkeypatch):
    org = active_openclaw
    steps = [
        _step(
            "oc_browser_1",
            "Open the vendor portal by hand",
            {"type": "browser.manual", "args": {"goal": "update the record"}},
            [],
        ),
        _step(
            "oc_browser_2",
            "Log the outcome in Notion",
            {
                "type": "notion.create_page",
                "args": {"parent": "PAGE_1", "title": "Portal log"},
            },
            ["oc_browser_1"],
        ),
    ]
    run_id = _make_run(org, "bot_browser", steps)
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)
    _approve_all(org, ["oc_browser_1", "oc_browser_2"])
    token = runtime.mint_capability(org, run_id)

    # The real _execute_side_effect runs here: browser fallback is manual and
    # must keep saying so instead of pretending it did the work.
    result = runtime.run_tool(
        token, "browser_fallback", {"action_id": "oc_browser_1", "step_id": "b1"}
    )

    assert result["ok"] is False
    assert result["status"] == "needs_attention"
    assert "manual" in result["result"]["error"]
    statuses = _statuses(org, run_id)
    assert statuses["oc_browser_1"] == "needs_attention"
    # needs_attention is terminal and unmet, so the dependent is settled too.
    assert statuses["oc_browser_2"] == "needs_attention"


# ------------------------------------------------------------- 7. HTTP edge


def test_tool_bridge_http_status_never_leaks_an_action_status(
    active_openclaw, monkeypatch
):
    """The smallest possible app around the OpenClaw router — not app.main."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.openclaw import router as openclaw_router

    org = active_openclaw
    run_id = _make_run(org, "bot_notion_http")
    _record_side_effect(monkeypatch)
    monkeypatch.setattr(runtime, "start_run_async", lambda *_a: None)
    _approve_all(org, ["oc_notion_1", "oc_notion_2"])
    token = runtime.mint_capability(org, run_id)

    app = FastAPI()
    app.include_router(openclaw_router.router)
    client = TestClient(app)

    # Out of order -> a real HTTP conflict, and the body explains it.
    early = client.post(
        "/openclaw/tools/pipedream_run_app_action",
        json={"action_id": "oc_notion_2", "step_id": "h2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert early.status_code == 409
    assert early.json()["code"] == runtime.DEPENDENCY_NOT_READY

    # A completed call is a 200 whose ACTION status stays in the body. Before
    # the router fix this raised ValueError on int("done") and 500'd.
    ok = client.post(
        "/openclaw/tools/pipedream_run_app_action",
        json={"action_id": "oc_notion_1", "step_id": "h1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    assert ok.json()["status"] == "done"

    unauthorized = client.post(
        "/openclaw/tools/pipedream_run_app_action",
        json={"action_id": "oc_notion_1", "step_id": "h1"},
        headers={"Authorization": "Bearer not-a-capability"},
    )
    assert unauthorized.status_code == 401
