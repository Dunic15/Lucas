"""OpenClaw over ANY connected app — the generic connected-app plane.

Key-free by construction: ``FakePipedream`` replaces the whole Connect surface
(accounts + proxy), so nothing here needs a Pipedream project, a plan tier or a
network. The fake is deliberately strict about tenancy — it refuses an account
id that does not belong to the ``external_user_id`` it was called with — so a
cross-tenant leak fails loudly instead of silently passing.

The eight scenarios the plane has to get right:

1. an app the org just connected is available (chat AND meeting surfaces);
2. an app the org has NOT connected is blocked;
3. a bounded read is allowed during planning, and redacted;
4. a write with no approval is refused;
5. an approved write executes exactly once;
6. a multi-step workflow keeps its dependencies and stays approval-gated;
7. SSRF / non-approved hosts are refused;
8. two orgs never see or touch each other's connections.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipedream_client, pipedream_executor, store
from app.actions import app_policy, app_registry, ledger, outbox
from app.brain import tool_registry
from app.config import settings
from app.openclaw import runtime

PROXY = pipedream_executor.PROXY_ACTION_TYPE
ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"


class FakePipedream:
    """A Connect account plane + proxy with real per-org isolation."""

    def __init__(self, accounts_by_org: dict[str, list[str]]):
        self.accounts_by_org = accounts_by_org
        self.proxy_calls: list[dict] = []
        self.response: dict = {"status": 200, "ok": True, "json": {"id": "obj_1"}}

    # ── the two client functions the plane actually uses ────────────────
    def list_accounts(self, external_user_id: str, *, app: str = "") -> list[dict]:
        slugs = self.accounts_by_org.get(str(external_user_id), [])
        return [
            {
                "id": f"acct_{external_user_id}_{slug}",
                "app": slug,
                "name": f"{slug} account",
                "healthy": True,
            }
            for slug in slugs
            if not app or slug == app
        ]

    def proxy_request(self, external_user_id, account_id, method, url, **kw):
        # Tenancy is not a convention here: an account id minted for another
        # org must never reach the proxy, so the fake refuses it outright.
        if not str(account_id).startswith(f"acct_{external_user_id}_"):
            raise AssertionError(
                f"cross-tenant proxy call: {account_id!r} used by {external_user_id!r}"
            )
        self.proxy_calls.append(
            {
                "org": external_user_id,
                "account_id": account_id,
                "method": method,
                "url": url,
                "body": kw.get("json_body"),
                "headers": kw.get("headers") or {},
            }
        )
        return dict(self.response)


def _install(monkeypatch, fake: FakePipedream) -> FakePipedream:
    monkeypatch.setattr(settings, "pipedream_project_id", "proj_test")
    monkeypatch.setattr(settings, "pipedream_client_id", "cid")
    monkeypatch.setattr(settings, "pipedream_client_secret", "sec")
    monkeypatch.setattr(settings, "pipedream_executor", True)
    monkeypatch.setattr(pipedream_client, "list_accounts", fake.list_accounts)
    monkeypatch.setattr(pipedream_client, "proxy_request", fake.proxy_request)
    # The pre-built component catalog is the plan-gated surface: it must never
    # be needed, so it is wired to fail here.
    monkeypatch.setattr(
        pipedream_client, "list_actions",
        lambda *_a, **_k: (_ for _ in ()).throw(
            pipedream_client.PipedreamError("not available on your current plan")
        ),
    )
    monkeypatch.setattr(pipedream_client, "plan_gated", lambda: True)
    pipedream_executor._reset_conn_cache()
    return fake


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    importlib.reload(outbox)
    runtime._SCHEMA_READY = False
    return store


@pytest.fixture
def openclaw(monkeypatch, fresh_store):
    monkeypatch.setattr(settings, "openclaw_experiment_enabled", True)
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", f"{ORG_A} {ORG_B}")
    monkeypatch.setattr(settings, "openclaw_auto_run", False)
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(settings, "connected_app_allow", "")
    monkeypatch.setattr(settings, "connected_app_deny", "")
    monkeypatch.setattr(settings, "connected_app_registry_extra", "")
    # No native adapter is connected in these scenarios: the point is the
    # GENERIC plane, so the catalog must stand on connected accounts alone.
    monkeypatch.setattr(runtime.native_runtime, "catalog", lambda _org: [])
    return ORG_A


def _proxy_action(action_id: str, org: str, **args) -> dict:
    return {
        "summary": "Follow up on the connected app.",
        "decisions": [],
        "actions": [
            {
                "action_id": action_id,
                "item": "Create the tracking issue",
                "owner": "OpenClaw",
                "typed": {"type": PROXY, "args": args},
            }
        ],
        "avatar_id": "laura",
        "org_id": org,
        "meeting_url": "https://meet.google.com/connected-apps",
    }


class _Avatar:
    id = "laura"
    drive_folder_id = ""

    @staticmethod
    def uses_native_tool(_name):
        return False


# ── 1. a just-connected app is available ────────────────────────────────────

def test_connected_app_is_available_to_chat_and_to_the_meeting(
    openclaw, monkeypatch
):
    """GitHub has a registry row but NO deterministic adapter and is named
    nowhere in the runtime — exactly the 'new app' case."""
    _install(monkeypatch, FakePipedream({ORG_A: ["github"]}))

    entry = app_policy.entry_for(ORG_A, "github")
    assert entry is not None
    assert (entry["can_read"], entry["can_write"]) == (True, True)
    assert entry["adapter_required"] is False
    assert entry["requires_approval"] is True
    assert entry["deterministic_types"] == []          # no adapter, still usable
    assert "api.github.com" in entry["api_hosts"]

    # Chat surface.
    apps, schemas = runtime._connected_action_context(ORG_A)
    assert apps == ["github"]
    assert PROXY in schemas

    catalog = runtime.tool_catalog(ORG_A)
    generic = [t for t in catalog if t["source"] == "pipedream_proxy"]
    assert [t["app"] for t in generic] == ["github"]
    assert generic[0]["requires_approval"] is True
    # The plan-gated pre-built catalog is unavailable, and nothing depends on it.
    assert not [t for t in catalog if t["action_type"] == "pd.<app>.run"]

    # Meeting surface — no capability row was ever set for this avatar.
    assert store.get_avatar_capabilities("laura", ORG_A) == {}
    reg = tool_registry.assemble(ORG_A, _Avatar())
    assert [a["slug"] for a in reg["pd_apps"]] == ["github"]
    assert reg["pd_apps"][0]["actions"] == ["read data", "create and update records"]
    assert "Github" in tool_registry.brief(reg)


def test_connected_app_without_a_registered_api_explains_its_limit(
    openclaw, monkeypatch
):
    """An app with no registry row is NOT silently dropped and NOT given a
    guessed host — it is listed with the reason it cannot be driven."""
    _install(monkeypatch, FakePipedream({ORG_A: ["acme_erp"]}))

    entry = app_policy.entry_for(ORG_A, "acme_erp")
    assert entry is not None and entry["adapter_required"] is True
    assert (entry["can_read"], entry["can_write"]) == (False, False)
    assert "no registered API surface" in entry["limit"]

    listed = [t for t in runtime.tool_catalog(ORG_A) if t["app"] == "acme_erp"]
    assert listed and listed[0]["source"] == "connected_app"
    assert "no registered API surface" in listed[0]["limit"]
    # It is never offered as a generic write target.
    assert not [
        t for t in runtime.tool_catalog(ORG_A)
        if t["app"] == "acme_erp" and t["source"] == "pipedream_proxy"
    ]
    assert any("acme_erp" in line for line in app_policy.planner_limits([entry]))

    # An operator can make it usable with data only — no deploy, no code.
    monkeypatch.setattr(
        settings, "connected_app_registry_extra",
        '{"acme_erp":{"label":"Acme ERP","hosts":["api.acme-erp.test"]}}',
    )
    entry = app_policy.entry_for(ORG_A, "acme_erp")
    assert entry["adapter_required"] is False and entry["can_write"] is True


# ── 2. an app the org has NOT connected is blocked ──────────────────────────

def test_unconnected_app_is_blocked_in_planning_and_at_execution(
    openclaw, monkeypatch
):
    fake = _install(monkeypatch, FakePipedream({ORG_A: ["github"]}))
    workflow = {
        "title": "Write to an app we never connected",
        "steps": [
            {
                "description": "Create a Notion page",
                "action_type": PROXY,
                "args": {
                    "app": "notion",
                    "method": "POST",
                    "url": "https://api.notion.com/v1/pages",
                    "body": {"properties": {}},
                },
            }
        ],
    }

    normalized = runtime._normalize_chat_workflow(ORG_A, workflow)
    assert normalized["ready"] is False
    assert "connected_app" in normalized["steps"][0]["missing_params"]
    assert "not connected" in normalized["steps"][0]["limit"]

    started = runtime.start_chat_workflow(ORG_A, workflow)
    assert started["ok"] is False and started["error"] == "needs_details"

    # And the planner cannot reach it either.
    read = pipedream_executor.read_proxy_for_planner(
        ORG_A,
        {"app": "notion", "method": "GET", "url": "https://api.notion.com/v1/users"},
    )
    assert read["ok"] is False and "isn't connected" in read["error"]
    assert fake.proxy_calls == []


# ── 3. a bounded read is allowed during planning ────────────────────────────

def test_planner_read_is_allowed_bounded_and_redacted(openclaw, monkeypatch):
    fake = _install(monkeypatch, FakePipedream({ORG_A: ["github", "notion"]}))
    fake.response = {
        "status": 200,
        "ok": True,
        "json": {
            "login": "acme",
            "access_token": "ghp_supersecret",     # must never come back
            "bio": "x" * 5000,                     # must be bounded
        },
    }

    read = pipedream_executor.read_proxy_for_planner(
        ORG_A,
        {"app": "github", "method": "GET", "url": "https://api.github.com/user"},
    )

    assert read["ok"] is True
    assert read["data"]["login"] == "acme"
    assert read["data"]["access_token"] == "[redacted]"
    assert len(read["data"]["bio"]) == 2000
    assert fake.proxy_calls[0]["account_id"] == f"acct_{ORG_A}_github"
    # A read is not a side effect: nothing entered the Action Center.
    assert ledger.action_statuses(["oc_none"], org_id=ORG_A) == {}

    # A write dressed as a planner read is refused...
    blocked = pipedream_executor.read_proxy_for_planner(
        ORG_A,
        {
            "app": "github",
            "method": "POST",
            "url": "https://api.github.com/repos/acme/api/issues",
            "body": {"title": "nope"},
        },
    )
    assert blocked["ok"] is False
    assert "may not perform this API operation" in blocked["error"]

    # ...while a POST the app's own registry row declares to BE a read is
    # allowed. This is the Notion special case, now generic data.
    searched = pipedream_executor.read_proxy_for_planner(
        ORG_A,
        {"app": "notion", "method": "POST", "url": "https://api.notion.com/v1/search"},
    )
    assert searched["ok"] is True
    assert fake.proxy_calls[-1]["headers"]["Notion-Version"] == (
        app_registry.spec_for("notion").default_headers()["Notion-Version"]
    )
    assert len(fake.proxy_calls) == 2   # the refused write never left the box


# ── 4. a write with no approval is refused ──────────────────────────────────

def test_write_without_approval_is_refused_by_the_tool_bridge(
    openclaw, monkeypatch
):
    fake = _install(monkeypatch, FakePipedream({ORG_A: ["github"]}))
    created = runtime.create_meeting_run(
        "bot_no_approval",
        _proxy_action(
            "oc_unapproved", ORG_A,
            app="github", method="POST",
            url="https://api.github.com/repos/acme/api/issues",
            body={"title": "Follow up"},
        ),
        ORG_A,
    )
    run_id = created["run"]["run_id"]
    token = runtime.mint_capability(ORG_A, run_id)

    # A valid run capability is NOT approval.
    result = runtime.run_tool(
        token,
        "pipedream_run_app_action",
        {"action_id": "oc_unapproved", "step_id": "step-1"},
    )

    assert result["ok"] is False
    assert result["status"] == 403
    assert "has not been approved" in result["error"]
    assert fake.proxy_calls == []
    detail = runtime.run_detail(ORG_A, run_id)
    assert detail["actions"][0]["status"] == "queued"


# ── 5. an approved write executes exactly once ──────────────────────────────

def test_approved_write_executes_exactly_once_with_a_receipt(
    openclaw, monkeypatch
):
    fake = _install(monkeypatch, FakePipedream({ORG_A: ["github"]}))
    fake.response = {
        "status": 201,
        "ok": True,
        "json": {"id": 42, "html_url": "https://github.com/acme/api/issues/42"},
    }
    created = runtime.create_meeting_run(
        "bot_approved",
        _proxy_action(
            "oc_approved", ORG_A,
            app="github", method="POST",
            url="https://api.github.com/repos/acme/api/issues",
            body={"title": "Follow up"},
        ),
        ORG_A,
    )
    run_id = created["run"]["run_id"]
    token = runtime.mint_capability(ORG_A, run_id)

    approved = runtime.approve_action(ORG_A, "oc_approved", start=False)
    assert approved["ok"] is True and approved["replay"] is False

    first = runtime.run_tool(
        token, "pipedream_run_app_action",
        {"action_id": "oc_approved", "step_id": "step-1"},
    )
    # A second attempt, with a DIFFERENT step id, must still not re-send.
    second = runtime.run_tool(
        token, "pipedream_run_app_action",
        {"action_id": "oc_approved", "step_id": "step-2"},
    )

    assert first["ok"] is True and first["replay"] is False
    assert second["ok"] is True and second["replay"] is True
    assert len(fake.proxy_calls) == 1
    assert fake.proxy_calls[0]["method"] == "POST"
    assert fake.proxy_calls[0]["body"] == {"title": "Follow up"}

    # A verifiable receipt on the canonical ledger, and a second approval
    # cannot re-claim execution.
    status = ledger.action_statuses(["oc_approved"], org_id=ORG_A)["oc_approved"]
    assert status["status"] == "done"
    assert runtime.approve_action(ORG_A, "oc_approved", start=False)["replay"] is True
    assert len(fake.proxy_calls) == 1


# ── 6. multi-step workflow with dependencies ────────────────────────────────

def test_multi_step_workflow_keeps_dependencies_and_stays_approval_gated(
    openclaw, monkeypatch
):
    fake = _install(monkeypatch, FakePipedream({ORG_A: ["github", "notion"]}))
    workflow = {
        "title": "Track the decision",
        "summary": "Open an issue, then record it in Notion.",
        "steps": [
            {
                "description": "Open the GitHub issue",
                "action_type": PROXY,
                "args": {
                    "app": "github",
                    "method": "POST",
                    "url": "https://api.github.com/repos/acme/api/issues",
                    "body": {"title": "Ship the pilot"},
                },
            },
            {
                "description": "Record it in Notion",
                "action_type": "notion.create_page",
                "args": {"title": "Pilot tracking", "content": "Issue opened."},
                "depends_on": [1],
            },
            {
                "description": "Comment back on the issue",
                "action_type": PROXY,
                "args": {
                    "app": "github",
                    "method": "POST",
                    "url": "https://api.github.com/repos/acme/api/issues/1/comments",
                    "body": {"body": "Tracked in Notion."},
                },
                "depends_on": [1, 2],
            },
        ],
    }

    normalized = runtime._normalize_chat_workflow(ORG_A, workflow)
    assert normalized["ready"] is True
    assert [s["depends_on"] for s in normalized["steps"]] == [[], [1], [1, 2]]
    assert all(s["requires_approval"] for s in normalized["steps"])
    # Proposing is not executing.
    assert fake.proxy_calls == []

    # "Refine in Chat" round-trips an archived draft without losing the graph.
    restored = runtime._restore_archived_chat_workflow(normalized)
    assert [s["depends_on"] for s in restored["steps"]] == [[], [1], [1, 2]]

    # The subject here is the approval + dependency graph, not the gateway
    # handoff. Stubbing the async start also keeps its daemon thread from
    # racing this test's temporary store to teardown.
    started_runs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runtime, "start_run_async",
        lambda org, run_id: started_runs.append((org, run_id)),
    )

    started = runtime.start_chat_workflow(
        ORG_A, workflow, laura_user_id="user-1"
    )
    assert started["ok"] is True
    assert len(started["actions"]) == 3
    assert started_runs == [(ORG_A, started["run_id"])]

    detail = runtime.run_detail(ORG_A, started["run_id"])
    actions = {a["action_id"]: a for a in detail["actions"]}
    assert all(a["status"] == "running" for a in actions.values())
    # Each step recorded an explicit decision — the user's Start click.
    for action_id in started["actions"]:
        decision = ledger.get_action_decision(action_id, org_id=ORG_A)
        assert decision["decision"] == "approve"

    # Dependencies survive into the canonical actions, by action id.
    payload = runtime.get_run(ORG_A, started["run_id"])["input"]
    ordered = sorted(payload["actions"], key=lambda a: a["sequence"])
    assert ordered[1]["depends_on"] == [ordered[0]["action_id"]]
    assert ordered[2]["depends_on"] == [
        ordered[0]["action_id"], ordered[1]["action_id"]
    ]


# ── 7. SSRF / non-approved hosts ────────────────────────────────────────────

@pytest.mark.parametrize(
    "args, expected",
    [
        ({"app": "github", "method": "GET",
          "url": "https://evil.test/steal"}, "approved API host"),
        ({"app": "github", "method": "GET",
          "url": "http://api.github.com/user"}, "plain HTTPS URL"),
        ({"app": "github", "method": "GET",
          "url": "https://api.github.com:8443/user"}, "plain HTTPS URL"),
        ({"app": "github", "method": "GET",
          "url": "https://user:pw@api.github.com/user"}, "plain HTTPS URL"),
        ({"app": "github", "method": "GET",
          "url": "https://169.254.169.254/latest/meta-data"}, "approved API host"),
        ({"app": "unknown_app", "method": "GET",
          "url": "https://api.github.com/user"}, "no registered API host"),
        ({"app": "github", "method": "TRACE",
          "url": "https://api.github.com/user"}, "API method must be"),
        ({"app": "github", "method": "GET", "url": "https://api.github.com/user",
          "headers": {"Authorization": "Bearer stolen"}}, "not allowed"),
        # A wildcard host may only ever widen to a real subdomain of the
        # vendor's own domain — never to a look-alike registration.
        ({"app": "jira", "method": "GET",
          "url": "https://evilatlassian.net/rest/api/3/myself"},
         "approved API host"),
    ],
)
def test_ssrf_and_disallowed_operations_are_refused(
    openclaw, monkeypatch, args, expected
):
    fake = _install(monkeypatch, FakePipedream({ORG_A: ["github", "jira"]}))

    with pytest.raises(ValueError, match=expected):
        pipedream_executor.validate_proxy_request_args(args)
    assert fake.proxy_calls == []


def test_wildcard_host_accepts_the_vendors_own_subdomain(openclaw, monkeypatch):
    _install(monkeypatch, FakePipedream({ORG_A: ["jira"]}))
    app, method, url, _body, _headers = (
        pipedream_executor.validate_proxy_request_args(
            {
                "app": "jira",
                "method": "GET",
                "url": "https://acme.atlassian.net/rest/api/3/myself",
            }
        )
    )
    assert (app, method) == ("jira", "GET")
    assert url.startswith("https://acme.atlassian.net/")


def test_policy_deny_blocks_an_operation_and_survives_the_executor(
    openclaw, monkeypatch
):
    fake = _install(monkeypatch, FakePipedream({ORG_A: ["github"]}))
    monkeypatch.setattr(settings, "connected_app_deny", "github:DELETE *")

    assert app_policy.evaluate("github", "POST", "/repos/a/b/issues").allow is True
    denied = app_policy.evaluate("github", "DELETE", "/repos/a/b/issues/1")
    assert denied.allow is False and denied.requires_approval is True

    # The deny is enforced where it matters: at execution, as a failed receipt.
    result = pipedream_executor.execute_for_openclaw(
        ORG_A, "oc_denied",
        {"type": PROXY, "args": {
            "app": "github", "method": "DELETE",
            "url": "https://api.github.com/repos/acme/api/issues/1",
        }},
    )
    assert result["ok"] is False
    assert "DELETE /repos/acme/api/issues/1 is not permitted on github" in (
        result["error"]
    )
    assert fake.proxy_calls == []


# ── 8. two orgs are isolated ────────────────────────────────────────────────

def test_two_orgs_never_see_or_touch_each_others_connections(
    openclaw, monkeypatch
):
    fake = _install(
        monkeypatch, FakePipedream({ORG_A: ["github"], ORG_B: ["notion"]})
    )

    assert [e["slug"] for e in app_policy.catalog(ORG_A)] == ["github"]
    assert [e["slug"] for e in app_policy.catalog(ORG_B)] == ["notion"]

    # Org A cannot read through org B's connection...
    blocked = pipedream_executor.read_proxy_for_planner(
        ORG_A,
        {"app": "notion", "method": "POST", "url": "https://api.notion.com/v1/search"},
    )
    assert blocked["ok"] is False and "isn't connected" in blocked["error"]

    # ...and each org's own read uses only its own account (the fake raises
    # on any mismatch).
    assert pipedream_executor.read_proxy_for_planner(
        ORG_A, {"app": "github", "method": "GET", "url": "https://api.github.com/user"}
    )["ok"] is True
    assert pipedream_executor.read_proxy_for_planner(
        ORG_B,
        {"app": "notion", "method": "POST", "url": "https://api.notion.com/v1/search"},
    )["ok"] is True
    assert {call["org"] for call in fake.proxy_calls} == {ORG_A, ORG_B}
    for call in fake.proxy_calls:
        assert call["account_id"] == f"acct_{call['org']}_" + (
            "github" if call["org"] == ORG_A else "notion"
        )

    # An org-B capability cannot drive an org-A action. Isolation bites at the
    # capability layer: org A's run simply does not exist in org B's namespace,
    # so the token never resolves and the bridge is never reached.
    created = runtime.create_meeting_run(
        "bot_org_a",
        _proxy_action(
            "oc_org_a", ORG_A,
            app="github", method="POST",
            url="https://api.github.com/repos/acme/api/issues",
            body={"title": "A only"},
        ),
        ORG_A,
    )
    runtime.approve_action(ORG_A, "oc_org_a", start=False)
    foreign = runtime.mint_capability(ORG_B, created["run"]["run_id"])

    result = runtime.run_tool(
        foreign,
        "pipedream_run_app_action",
        {"action_id": "oc_org_a", "step_id": "step-1"},
    )

    assert result["ok"] is False
    assert (result["status"], result["error"]) == (401, "invalid capability")
    assert runtime.run_detail(ORG_B, created["run"]["run_id"]) is None
    assert runtime.get_run(ORG_B, created["run"]["run_id"]) is None
    # The approved org-A write was never executed by the foreign caller.
    assert [c for c in fake.proxy_calls if "/issues" in c["url"]] == []
