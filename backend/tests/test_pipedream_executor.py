"""P1 — Pipedream Connect-Proxy execution plane + per-family routing.

Key-free: pipedream_client (list_accounts/proxy_request) and the ledger are
monkeypatched; no real Pipedream or network calls. Asserts:
  * per-family routing (executor.route_for_typed): Asana→pipedream when the flag
    is on, Google/Slack→native, untyped→cedric, and byte-identical (Asana→native)
    when the flag is off;
  * the Asana request builders faithfully mirror asana_client;
  * execute_approved writes done/failed receipts and never raises;
  * both approve doors (dashboard + org) dispatch route='pipedream' through the
    Pipedream plane behind the exactly-once claim, and never double-run native.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import (
    auth, avatar_resolver, executor, ledger, org_api, pipedream_client,
    pipedream_executor, store,
)
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


def _login(client: TestClient, email: str = "owner@x.com") -> dict:
    user = store.upsert_user(email)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def _enable_pd(monkeypatch):
    monkeypatch.setattr(settings, "pipedream_project_id", "proj_test")
    monkeypatch.setattr(settings, "pipedream_client_id", "cid")
    monkeypatch.setattr(settings, "pipedream_client_secret", "sec")
    monkeypatch.setattr(settings, "pipedream_executor", True)
    monkeypatch.setattr(settings, "native_executor", True)


def _cap_ledger(monkeypatch) -> dict:
    seen: dict = {}

    def fake_status(aid, status, detail, org_id="", receipt=None):
        seen.update(status=status, detail=detail, receipt=receipt, aid=aid)

    monkeypatch.setattr(pipedream_executor.ledger, "set_action_status", fake_status)
    return seen


# ── routing: executor.route_for_typed (the per-family decision) ─────────────

def test_route_off_keeps_asana_native(monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(settings, "pipedream_executor", False)
    assert executor.route_for_typed({"type": "asana.create_task", "args": {"name": "x"}}) == "native"


def test_route_on_sends_asana_to_pipedream_google_slack_stay_native(monkeypatch):
    _enable_pd(monkeypatch)
    assert executor.route_for_typed({"type": "asana.create_task", "args": {"name": "x"}}) == "pipedream"
    assert executor.route_for_typed({"type": "asana.update_task", "args": {"task": "1", "name": "y"}}) == "pipedream"
    # The whole Google block stays native; Slack stays native (Cedric owns the app).
    assert executor.route_for_typed({"type": "calendar.create_event", "args": {}}) == "native"
    assert executor.route_for_typed({"type": "email.send", "args": {}}) == "native"
    assert executor.route_for_typed({"type": "slack.post_message", "args": {"text": "hi"}}) == "native"
    # Untyped / non-native → Cedric.
    assert executor.route_for_typed(None) == "cedric"
    assert executor.route_for_typed({"type": "weird.unknown"}) == "cedric"


def test_route_pipedream_requires_config_not_just_flag(monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(settings, "pipedream_executor", True)
    monkeypatch.setattr(settings, "pipedream_project_id", "")  # unconfigured
    # Flag on but not configured ⇒ enabled() False ⇒ Asana falls back to native.
    assert executor.route_for_typed({"type": "asana.create_task", "args": {"name": "x"}}) == "native"


# ── gating ──────────────────────────────────────────────────────────────────

def test_handles_gated_by_flag_and_config(monkeypatch):
    monkeypatch.setattr(settings, "pipedream_executor", False)
    assert pipedream_executor.enabled() is False
    assert pipedream_executor.handles({"type": "asana.create_task"}) is False
    _enable_pd(monkeypatch)
    assert pipedream_executor.enabled() is True
    assert pipedream_executor.handles({"type": "asana.create_task"}) is True
    # Pipedream does NOT own Google — that stays native.
    assert pipedream_executor.handles({"type": "email.send"}) is False


# ── Asana request builders (faithful to asana_client) ───────────────────────

def test_asana_create_with_project_gid_skips_workspace(monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipedream_client, "proxy_request",
                        lambda *a, **k: calls.append(a) or {"ok": True})
    method, url, body, hdr = pipedream_executor._build_asana_create(
        "org", "apn_1", {"name": "Ship", "project": "777", "notes": "n"})
    assert method == "POST" and "/tasks?opt_fields=" in url
    assert body["data"]["projects"] == ["777"] and "workspace" not in body["data"]
    assert body["data"]["assignee"] == "me" and body["data"]["notes"] == "n"
    assert calls == []  # no workspace lookup when a project gid is supplied


def test_asana_create_without_project_resolves_workspace(monkeypatch):
    def fake_proxy(org, acct, method, url, json_body=None, headers=None):
        assert method == "GET" and url.endswith("/workspaces")
        return {"ok": True, "json": {"data": [{"gid": "WS1"}]}}

    monkeypatch.setattr(pipedream_client, "proxy_request", fake_proxy)
    _m, _u, body, _h = pipedream_executor._build_asana_create("org", "apn_1", {"name": "Ship"})
    assert body["data"]["workspace"] == "WS1"


def test_asana_create_missing_name_raises():
    with pytest.raises(ValueError):
        pipedream_executor._build_asana_create("org", "apn", {"project": "1"})


def test_asana_update_and_comment_builders():
    m, u, b, _h = pipedream_executor._build_asana_update("o", "a", {"task": "42", "completed": True})
    assert m == "PUT" and "/tasks/42" in u and b["data"]["completed"] is True
    m2, u2, b2, _h2 = pipedream_executor._build_asana_comment("o", "a", {"task": "42", "text": "hi"})
    assert m2 == "POST" and u2.endswith("/tasks/42/stories") and b2["data"]["text"] == "hi"
    with pytest.raises(ValueError):
        pipedream_executor._build_asana_comment("o", "a", {"task": "42"})  # no text
    with pytest.raises(ValueError):
        pipedream_executor._build_asana_update("o", "a", {"task": "42"})  # no changes
    with pytest.raises(ValueError):
        pipedream_executor._build_asana_update("o", "a", {"task": "abc"})  # non-numeric gid


# ── execute_approved end-to-end (mocked proxy + ledger) ─────────────────────

def test_execute_approved_happy_writes_done_receipt(monkeypatch):
    _enable_pd(monkeypatch)
    monkeypatch.setattr(pipedream_client, "list_accounts",
                        lambda org, app="": [{"id": "apn_9", "app": "asana", "healthy": True}])
    captured: dict = {}

    def fake_proxy(org, acct, method, url, json_body=None, headers=None):
        captured.update(org=org, acct=acct, method=method, url=url, body=json_body)
        return {"ok": True, "status": 200,
                "json": {"data": {"gid": "55", "permalink_url": "https://app.asana.com/0/0/55/f"}}}

    monkeypatch.setattr(pipedream_client, "proxy_request", fake_proxy)
    seen = _cap_ledger(monkeypatch)
    out = pipedream_executor.execute_approved(
        "orgX", "act1", {"type": "asana.create_task", "task": {"name": "Ship", "project": "1"}})
    assert out["ok"] is True and out["ref"].endswith("/55/f") and out["kind"] == "asana task"
    assert captured["acct"] == "apn_9" and captured["method"] == "POST"
    assert seen["status"] == "done" and seen["receipt"]["route"] == "pipedream"


def test_execute_approved_no_account_fails(monkeypatch):
    _enable_pd(monkeypatch)
    monkeypatch.setattr(pipedream_client, "list_accounts", lambda org, app="": [])
    seen = _cap_ledger(monkeypatch)
    out = pipedream_executor.execute_approved(
        "orgX", "act1", {"type": "asana.create_task", "task": {"name": "x"}})
    assert out["ok"] is False and "isn't connected" in out["error"]
    assert seen["status"] == "failed"


def test_execute_approved_downstream_error_fails(monkeypatch):
    _enable_pd(monkeypatch)
    monkeypatch.setattr(pipedream_client, "list_accounts",
                        lambda org, app="": [{"id": "apn_9", "healthy": True}])
    monkeypatch.setattr(pipedream_client, "proxy_request",
                        lambda *a, **k: {"ok": False, "status": 403, "json": None})
    seen = _cap_ledger(monkeypatch)
    out = pipedream_executor.execute_approved(
        "orgX", "act1", {"type": "asana.create_task", "task": {"name": "x", "project": "1"}})
    assert out["ok"] is False and "403" in out["error"]
    assert seen["status"] == "failed"


def test_execute_approved_bad_args_never_reaches_proxy(monkeypatch):
    _enable_pd(monkeypatch)
    monkeypatch.setattr(pipedream_client, "list_accounts",
                        lambda org, app="": [{"id": "apn_9", "healthy": True}])
    reached = {"proxy": False}
    monkeypatch.setattr(pipedream_client, "proxy_request",
                        lambda *a, **k: reached.update(proxy=True) or {"ok": True})
    seen = _cap_ledger(monkeypatch)
    out = pipedream_executor.execute_approved(
        "orgX", "act1", {"type": "asana.create_task", "task": {}})  # no name
    assert out["ok"] is False and "name" in out["error"]
    assert reached["proxy"] is False and seen["status"] == "failed"


def test_execute_approved_off_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "pipedream_executor", False)
    out = pipedream_executor.execute_approved(
        "o", "a", {"type": "asana.create_task", "task": {"name": "x"}})
    assert out["ok"] is False and out.get("skipped")


def test_execute_approved_pipedream_unreachable_fails(monkeypatch):
    _enable_pd(monkeypatch)

    def boom(org, app=""):
        raise pipedream_client.PipedreamError("down")

    monkeypatch.setattr(pipedream_client, "list_accounts", boom)
    seen = _cap_ledger(monkeypatch)
    out = pipedream_executor.execute_approved(
        "orgX", "act1", {"type": "asana.create_task", "task": {"name": "x"}})
    assert out["ok"] is False and "couldn't reach Pipedream" in out["error"]
    assert seen["status"] == "failed"


# ── dashboard door: route='pipedream' dispatches to Pipedream, not native ───

def _seed_pipedream_action(org: str, aid: str = "a1") -> None:
    action = {
        "item": "Create the ship task", "owner": "Ben", "action_id": aid,
        "typed": {"type": "asana.create_task",
                  "args": {"name": "Ship", "project": "1"}},
        "execution_route": "pipedream",  # what finalize stamps when the flag is on
    }
    store.save_artifact(
        f"bot_{aid}",
        {"summary": "s", "actions": [action], "checklist": [action],
         "org_id": org, "avatar_id": "laura",
         "meeting_url": "https://meet.google.com/x", "transcript": "PII never leaks"},
        org_id=org,
    )


def test_dashboard_approve_routes_asana_to_pipedream(client, monkeypatch):
    _enable_pd(monkeypatch)
    user = _login(client)
    _seed_pipedream_action(user["org_id"])
    monkeypatch.setattr(store, "get_avatar_capabilities", lambda a: {})
    monkeypatch.setattr(avatar_resolver, "family_allowed", lambda *a, **k: True)
    pd = {"n": 0}
    native = {"n": 0}
    monkeypatch.setattr(pipedream_executor, "execute_approved",
                        lambda o, aid, act: pd.update(n=pd["n"] + 1) or
                        {"ok": True, "kind": "asana task", "ref": "https://app.asana.com/0/0/9/f"})
    monkeypatch.setattr(executor, "execute_approved",
                        lambda o, aid, act: native.update(n=native["n"] + 1) or {"ok": True})
    r = client.post("/dashboard/actions/a1/approve",
                    headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is True
    assert pd["n"] == 1 and native["n"] == 0  # pipedream ran, native did NOT


def test_dashboard_approve_capability_blocked_does_not_run_pipedream(client, monkeypatch):
    _enable_pd(monkeypatch)
    user = _login(client)
    _seed_pipedream_action(user["org_id"])
    # The acting avatar's asana toggle is explicitly OFF ⇒ blocked, no execution.
    monkeypatch.setattr(store, "get_avatar_capabilities", lambda a: {"asana": False})
    pd = {"n": 0}
    monkeypatch.setattr(pipedream_executor, "execute_approved",
                        lambda o, aid, act: pd.update(n=pd["n"] + 1) or {"ok": True})
    r = client.post("/dashboard/actions/a1/approve",
                    headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 200
    body = r.json()
    assert body["capability_blocked"] is True and body["executed"] is False
    assert pd["n"] == 0


# ── org door: _execute_route dispatches route='pipedream' ───────────────────

def test_org_door_dispatches_asana_to_pipedream(monkeypatch):
    _enable_pd(monkeypatch)
    monkeypatch.setattr(settings, "action_dispatch_async", False)
    monkeypatch.setattr(org_api.store, "get_avatar_capabilities", lambda a: {})
    monkeypatch.setattr(avatar_resolver, "family_allowed", lambda *a, **k: True)
    monkeypatch.setattr(org_api.ledger, "claim_action_execution", lambda *a, **k: True)
    pd = {"n": 0}
    native = {"n": 0}
    monkeypatch.setattr(org_api.pipedream_executor, "execute_approved",
                        lambda o, aid, act: pd.update(n=pd["n"] + 1) or {"ok": True})
    monkeypatch.setattr(org_api.executor, "execute_approved",
                        lambda o, aid, act: native.update(n=native["n"] + 1) or {"ok": True})
    action = {"execution_route": "pipedream",
              "typed": {"type": "asana.create_task", "args": {"name": "x", "project": "1"}}}
    job, status, blocked = org_api._execute_route(
        "org", "act1", action, "laura", idempotency_key="k", via="test")
    assert status == "done" and blocked is False
    assert pd["n"] == 1 and native["n"] == 0


# ── app_connected probe (availability gate) ─────────────────────────────────

def test_app_connected_off_is_false(monkeypatch):
    monkeypatch.setattr(settings, "pipedream_executor", False)
    pipedream_executor._reset_conn_cache()
    assert pipedream_executor.app_connected("org", "asana") is False


def test_app_connected_true_and_cached(monkeypatch):
    _enable_pd(monkeypatch)
    pipedream_executor._reset_conn_cache()
    calls = {"n": 0}

    def fake_list(org, app=""):
        calls["n"] += 1
        return [{"id": "apn_1", "app": "asana", "healthy": True}]

    monkeypatch.setattr(pipedream_client, "list_accounts", fake_list)
    assert pipedream_executor.app_connected("orgX", "asana") is True
    assert pipedream_executor.app_connected("orgX", "asana") is True  # cached
    assert calls["n"] == 1  # one probe, then served from cache


def test_app_connected_false_when_no_account(monkeypatch):
    _enable_pd(monkeypatch)
    pipedream_executor._reset_conn_cache()
    monkeypatch.setattr(pipedream_client, "list_accounts", lambda org, app="": [])
    assert pipedream_executor.app_connected("orgX", "asana") is False


def test_app_connected_transient_error_not_cached(monkeypatch):
    _enable_pd(monkeypatch)
    pipedream_executor._reset_conn_cache()

    def boom(org, app=""):
        raise pipedream_client.PipedreamError("down")

    monkeypatch.setattr(pipedream_client, "list_accounts", boom)
    assert pipedream_executor.app_connected("orgX", "asana") is False
    # recovers — a transient failure is not stuck as a cached False
    monkeypatch.setattr(pipedream_client, "list_accounts",
                        lambda org, app="": [{"id": "apn_9"}])
    assert pipedream_executor.app_connected("orgX", "asana") is True


def test_avatar_asana_enabled_counts_pipedream_when_native_gone(monkeypatch):
    from app import asana_client, avatars, store as store_mod

    # Native Asana disconnected; Pipedream Asana connected.
    monkeypatch.setattr(asana_client, "connected", lambda org: False)
    monkeypatch.setattr(pipedream_executor, "app_connected",
                        lambda org, app: app == "asana")

    class _Av:
        def uses_native_tool(self, t):
            return t == "asana"

    monkeypatch.setattr(avatars, "load", lambda aid: _Av())
    monkeypatch.setattr(store_mod, "capability_enabled",
                        lambda aid, fam, connected=False: True)
    assert main_module._avatar_asana_enabled("orgX", "petra") is True
    # Neither native nor Pipedream connected → not available.
    monkeypatch.setattr(pipedream_executor, "app_connected", lambda org, app: False)
    assert main_module._avatar_asana_enabled("orgX", "petra") is False
