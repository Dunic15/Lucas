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


def test_route_falls_back_to_native_until_connected_in_pipedream(monkeypatch):
    _enable_pd(monkeypatch)
    # Connection-aware + multi-tenant safe: with nothing connected in Pipedream
    # (no org), every type that HAS a native adapter falls back to native — so a
    # native-era org / un-migrated external user is never broken (Ananth fix).
    assert executor.route_for_typed({"type": "asana.create_task", "args": {"name": "x"}}) == "native"
    assert executor.route_for_typed({"type": "asana.update_task", "args": {"task": "1", "name": "y"}}) == "native"
    assert executor.route_for_typed({"type": "calendar.create_event", "args": {}}) == "native"
    assert executor.route_for_typed({"type": "email.send", "args": {}}) == "native"
    assert executor.route_for_typed({"type": "slack.post_message", "args": {"text": "hi"}}) == "native"
    # Untyped / non-native → Cedric.
    assert executor.route_for_typed(None) == "cedric"
    assert executor.route_for_typed({"type": "weird.unknown"}) == "cedric"


def test_route_to_pipedream_once_connected_there(monkeypatch):
    _enable_pd(monkeypatch)
    # Org connected Gmail + Calendar + Asana in Pipedream → those route there;
    # a type NOT connected in Pipedream still falls back to native.
    monkeypatch.setattr(pipedream_executor, "app_connected",
                        lambda org, app: org == "org7" and app in {"gmail", "google_calendar", "asana"})
    assert executor.route_for_typed({"type": "email.send", "args": {}}, "org7") == "pipedream"
    assert executor.route_for_typed({"type": "calendar.create_event", "args": {}}, "org7") == "pipedream"
    assert executor.route_for_typed({"type": "asana.create_task", "args": {"name": "x"}}, "org7") == "pipedream"
    # Different org (nothing connected in Pipedream) → native fallback, not a failure.
    assert executor.route_for_typed({"type": "asana.create_task", "args": {"name": "x"}}, "orgX") == "native"
    # A different org (nothing connected) still falls back to native.
    assert executor.route_for_typed({"type": "email.send", "args": {}}, "orgX") == "native"


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
    # Pipedream now OWNS the Google block too (Gmail + Calendar), so it handles
    # those types; routing (route_for_typed) still gates them on a live
    # Pipedream connection during the cutover.
    assert pipedream_executor.handles({"type": "email.send"}) is True
    assert pipedream_executor.handles({"type": "calendar.create_event"}) is True
    assert pipedream_executor.is_google_type("email.send") is True
    assert pipedream_executor.is_google_type("asana.create_task") is False
    assert pipedream_executor.app_for_type("calendar.create_event") == "google_calendar"


# ── Google request builders (Gmail + Calendar via REST proxy) ───────────────

def test_gmail_send_builder_encodes_mime():
    import base64
    m, u, b, _h = pipedream_executor._build_gmail_send(
        "org", "apn", {"to": "a@b.com, c@d.com", "subject": "Hi", "body": "hello"})
    assert m == "POST" and u.endswith("/messages/send")
    raw = base64.urlsafe_b64decode(b["raw"].encode()).decode()
    assert "To: a@b.com, c@d.com" in raw and "Subject: Hi" in raw
    # Body rides as a base64 MIME payload — decode it back to confirm.
    import email as _email
    msg = _email.message_from_string(raw)
    assert msg.get_payload(decode=True).decode() == "hello"


def test_gmail_send_requires_recipient():
    with pytest.raises(ValueError):
        pipedream_executor._build_gmail_send("o", "a", {"subject": "x", "body": "y"})


def test_gmail_draft_wraps_message():
    m, u, b, _h = pipedream_executor._build_gmail_draft(
        "o", "a", {"to": "a@b.com", "subject": "s", "body": "t"})
    assert m == "POST" and u.endswith("/drafts") and "raw" in b["message"]


def test_calendar_create_builder_sets_meet_and_attendees():
    m, u, b, _h = pipedream_executor._build_calendar_create(
        "o", "a", {"title": "Sync", "start": "2026-07-24T15:00:00",
                   "end": "2026-07-24T15:30:00", "attendees": "x@y.com",
                   "timezone": "Europe/Rome"})
    assert m == "POST" and "conferenceDataVersion=1" in u and "sendUpdates=all" in u
    assert b["summary"] == "Sync"
    assert b["start"] == {"dateTime": "2026-07-24T15:00:00", "timeZone": "Europe/Rome"}
    assert b["attendees"] == [{"email": "x@y.com"}]
    assert b["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"


def test_calendar_create_requires_title_start_end():
    with pytest.raises(ValueError):
        pipedream_executor._build_calendar_create("o", "a", {"title": "x", "start": "t"})


def test_calendar_update_patches_only_changes():
    m, u, b, _h = pipedream_executor._build_calendar_update(
        "o", "a", {"event_id": "ev1", "start": "2026-07-24T16:00:00"})
    assert m == "PATCH" and "/events/ev1" in u
    assert b["start"]["dateTime"] == "2026-07-24T16:00:00" and "summary" not in b


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
    calls: list = []

    def fake_proxy(org, acct, method, url, json_body=None, headers=None):
        calls.append({"org": org, "acct": acct, "method": method,
                      "url": url, "body": json_body})
        return {"ok": True, "status": 200,
                "json": {"data": {"gid": "55", "permalink_url": "https://app.asana.com/0/0/55/f"}}}

    monkeypatch.setattr(pipedream_client, "proxy_request", fake_proxy)
    seen = _cap_ledger(monkeypatch)
    out = pipedream_executor.execute_approved(
        "orgX", "act1", {"type": "asana.create_task", "task": {"name": "Ship", "project": "1"}})
    # Phase-1 read-back: the WRITE is followed by a verify GET of the created
    # object, and the receipt carries the '· verified' evidence suffix.
    assert out["ok"] is True and out["ref"].endswith("/55/f")
    assert out["kind"] == "asana task · verified"
    write = calls[0]
    assert write["acct"] == "apn_9" and write["method"] == "POST"
    assert any(c["method"] == "GET" and "/tasks/55" in c["url"] for c in calls[1:])
    assert seen["status"] == "done" and seen["receipt"]["route"] == "pipedream"
    assert out["verified"] is True
    assert seen["receipt"]["verified"] is True


def test_asana_comment_verifies_the_story_not_a_task(monkeypatch):
    _enable_pd(monkeypatch)
    monkeypatch.setattr(pipedream_client, "list_accounts",
                        lambda org, app="": [{"id": "apn_9", "app": "asana", "healthy": True}])
    calls: list[tuple[str, str]] = []

    def fake_proxy(org, acct, method, url, json_body=None, headers=None):
        calls.append((method, url))
        return {"ok": True, "status": 200, "json": {"data": {"gid": "story55"}}}

    monkeypatch.setattr(pipedream_client, "proxy_request", fake_proxy)
    seen = _cap_ledger(monkeypatch)
    out = pipedream_executor.execute_approved(
        "orgX", "act-comment",
        {"type": "asana.add_comment", "task": {"task": "42", "text": "Ship it"}},
    )
    assert out["ok"] is True and out["verified"] is True
    assert calls[0][0] == "POST"
    assert calls[1][0] == "GET" and "/stories/story55" in calls[1][1]
    assert "/tasks/story55" not in calls[1][1]
    assert seen["receipt"]["verified"] is True


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


# ── dry_run (no-ledger) + /dashboard/test/integrations ──────────────────────

def test_dry_run_happy_no_ledger(monkeypatch):
    _enable_pd(monkeypatch)
    monkeypatch.setattr(pipedream_client, "list_accounts",
                        lambda org, app="": [{"id": "apn_1", "healthy": True}])
    monkeypatch.setattr(pipedream_client, "proxy_request",
                        lambda *a, **k: {"ok": True, "status": 200,
                                         "json": {"data": {"gid": "7", "permalink_url": "https://app.asana.com/0/0/7/f"}}})
    # dry_run must NOT touch the ledger
    def boom(*a, **k):
        raise AssertionError("dry_run wrote to the ledger")
    monkeypatch.setattr(pipedream_executor.ledger, "set_action_status", boom)
    out = pipedream_executor.dry_run("orgX", {"type": "asana.create_task", "task": {"name": "x", "project": "1"}})
    assert out["ok"] is True and out["route"] == "pipedream" and out["ref"].endswith("/7/f")


def test_dry_run_no_account(monkeypatch):
    _enable_pd(monkeypatch)
    monkeypatch.setattr(pipedream_client, "list_accounts", lambda org, app="": [])
    out = pipedream_executor.dry_run("orgX", {"type": "asana.create_task", "task": {"name": "x"}})
    assert out["ok"] is False and "isn't connected" in out["error"]


def test_test_integrations_requires_login(client):
    r = client.post("/dashboard/test/integrations", json={"email": "a@b.com"})
    assert r.status_code == 401


def test_test_integrations_runs_all_three(client, monkeypatch):
    _enable_pd(monkeypatch)
    from app import auth as _auth
    user = store.upsert_user("owner@x.com")
    client.cookies.set(_auth.COOKIE_NAME, _auth.make_cookie(user["user_id"]))
    from app import google_client
    seen = {}
    monkeypatch.setattr(google_client, "create_calendar_event",
                        lambda org, ev: (seen.update(cal=ev), {"ok": True, "event_url": "https://cal/1"})[1])
    monkeypatch.setattr(google_client, "send_gmail",
                        lambda org, m: (seen.update(mail=m), {"ok": True, "message_id": "m1"})[1])
    monkeypatch.setattr(pipedream_executor, "dry_run",
                        lambda org, a: {"ok": True, "kind": "asana task", "ref": "https://app.asana.com/x", "route": "pipedream"})
    r = client.post("/dashboard/test/integrations", json={"email": "duccio@sffstudio.com"},
                    headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True and b["target"] == "duccio@sffstudio.com"
    assert b["results"]["calendar"]["event_url"] == "https://cal/1"
    assert b["results"]["asana"]["route"] == "pipedream"
    assert seen["cal"]["attendees"] == ["duccio@sffstudio.com"]


def test_test_integrations_needs_email(client, monkeypatch):
    _enable_pd(monkeypatch)
    from app import auth as _auth
    user = store.upsert_user("owner@x.com")
    client.cookies.set(_auth.COOKIE_NAME, _auth.make_cookie(user["user_id"]))
    r = client.post("/dashboard/test/integrations", json={"email": "notanemail"},
                    headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 400


def test_untyped_route_is_manual_without_cedric(monkeypatch):
    """Owner rule 2026-07-22 ('Cedric lives inside Slack'): an org with NO
    connected cedric-brain never gets a 'cedric' stamp — untyped work becomes
    a track-only 'manual' card. Linked orgs, the demo org and the no-org
    legacy path keep the Slack-agent route."""
    from app import store as store_mod
    from app.config import settings

    monkeypatch.setattr(store_mod, "connections_for_org", lambda org: [])
    assert executor.route_for_typed(None, "org-real") == "manual"
    assert executor.route_for_typed({"type": "weird.unknown"}, "org-real") == "manual"
    # Even an explicit Slack ask stays manual when nothing is linked.
    assert executor.route_for_typed(
        None, "org-real", item_text="post the recap to Slack") == "manual"
    monkeypatch.setattr(
        store_mod, "connections_for_org",
        lambda org: [{"provider": "cedric-brain", "status": "connected"}],
    )
    # Linked org: ONLY an explicit Slack ask reaches the Slack agent
    # (owner rule 2026-07-22 part two — Cedric is never the catch-all).
    assert executor.route_for_typed(
        None, "org-real", item_text="post the recap to Slack") == "cedric"
    assert executor.route_for_typed(
        None, "org-real", item_text="share it in #general please") == "cedric"
    assert executor.route_for_typed(
        None, "org-real", item_text="sort out the vendor situation") == "manual"
    assert executor.route_for_typed(None, "org-real") == "manual"
    # Demo / no-org service scope keeps the legacy catch-all.
    assert executor.route_for_typed(None, settings.demo_org_id) == "cedric"
    assert executor.route_for_typed(None) == "cedric"


def test_read_calendar_events_via_proxy(monkeypatch):
    """The calendar-brief Pipedream fallback: read-only upcoming events through
    the Connect proxy, same {'ok','events'} contract as the native reader."""
    _enable_pd(monkeypatch)
    monkeypatch.setattr(pipedream_client, "enabled", lambda: True)
    monkeypatch.setattr(
        pipedream_client, "list_accounts",
        lambda org, app="": [{"id": "apn_1", "healthy": True}],
    )
    seen = {}

    def fake_proxy(org, acct, method, url, **kw):
        seen.update(org=org, acct=acct, method=method, url=url)
        return {"ok": True, "json": {"items": [
            {"summary": "Standup", "start": {"dateTime": "2026-07-23T09:00:00Z"}},
        ]}}

    monkeypatch.setattr(pipedream_client, "proxy_request", fake_proxy)
    res = pipedream_executor.read_calendar_events("org9", max_results=5)
    assert res["ok"] and res["events"][0]["summary"] == "Standup"
    assert seen["org"] == "org9" and seen["acct"] == "apn_1"
    assert seen["method"] == "GET"
    assert "maxResults=5" in seen["url"] and "singleEvents=true" in seen["url"]
    # Not connected in Pipedream → clean error, never a raise.
    monkeypatch.setattr(pipedream_client, "list_accounts", lambda org, app="": [])
    out = pipedream_executor.read_calendar_events("org9")
    assert not out["ok"] and "not connected" in out["error"]


# ── the 20-action expansion (2026-07-22) ────────────────────────────────────

def test_every_mapped_type_has_schema_risk_and_labels():
    """The canonical contract: NO action type ships without its schema, its
    risk class and human labels — the lesson of the 'manual' constraint
    incident, applied to the whole registry."""
    from app.actions import action_plane as ap

    for t in pipedream_executor._MAPPER:
        assert t in ap.PARAMS_SCHEMAS, f"{t} missing schema"
        assert t in ap.RISK_BY_TYPE, f"{t} missing risk class"
        for f in ap.PARAMS_SCHEMAS[t]:
            assert f.get("label"), f"{t}.{f.get('name')} missing label"
            assert f.get("label_it"), f"{t}.{f.get('name')} missing label_it"
    assert len(pipedream_executor._MAPPER) == 20


def test_asana_extra_builders():
    with pytest.raises(ValueError):
        pipedream_executor._build_asana_add_subtask("o", "a", {"task": "abc", "name": "x"})
    m, u, b, _ = pipedream_executor._build_asana_add_subtask(
        "o", "a", {"task": "42", "name": "step one"})
    assert m == "POST" and "/tasks/42/subtasks" in u and b["data"]["name"] == "step one"


def test_gmail_reply_builder_threads_correctly(monkeypatch):
    calls = []

    def fake_proxy(org, acct, method, url, json_body=None, headers=None):
        calls.append(url)
        if "/messages?q=" in url:
            return {"ok": True, "json": {"messages": [{"id": "m77"}]}}
        if "/messages/m77?format=metadata" in url:
            return {"ok": True, "json": {
                "threadId": "t9",
                "payload": {"headers": [
                    {"name": "Subject", "value": "Budget"},
                    {"name": "Message-ID", "value": "<abc@mail>"}]}}}
        return {"ok": True, "json": {}}

    monkeypatch.setattr(pipedream_client, "proxy_request", fake_proxy)
    m, u, b, _ = pipedream_executor._build_gmail_reply(
        "o", "a", {"to": "anant@sff.com", "body": "Sounds good"})
    assert m == "POST" and u.endswith("/messages/send")
    assert b["threadId"] == "t9"
    import base64 as b64

    raw = b64.urlsafe_b64decode(b["raw"] + "==").decode()
    assert "Re: Budget" in raw and "In-Reply-To: <abc@mail>" in raw
    with pytest.raises(ValueError):
        pipedream_executor._build_gmail_reply("o", "a", {"to": "x@y.z"})  # no body


def test_calendar_cancel_resolves_single_upcoming_event(monkeypatch):
    def fake_proxy(org, acct, method, url, json_body=None, headers=None):
        return {"ok": True, "json": {"items": [
            {"id": "ev1", "summary": "Pipedream sync",
             "start": {"dateTime": "2026-07-24T15:00:00Z"}},
            {"id": "ev2", "summary": "Team lunch",
             "start": {"dateTime": "2026-07-25T12:00:00Z"}}]}}

    monkeypatch.setattr(pipedream_client, "proxy_request", fake_proxy)
    m, u, b, _ = pipedream_executor._build_calendar_cancel(
        "o", "a", {"title": "Pipedream sync"})
    assert m == "DELETE" and "/events/ev1" in u and "sendUpdates=all" in u
    # ambiguous title → honest refusal listing candidates
    with pytest.raises(ValueError) as e:
        pipedream_executor._build_calendar_cancel("o", "a", {"title": "e"})
    assert "matches 2" in str(e.value)


def test_drive_share_and_move_builders(monkeypatch):
    def fake_proxy(org, acct, method, url, json_body=None, headers=None):
        if "files?q=name contains 'Recap" in url:
            return {"ok": True, "json": {"files": [
                {"id": "f1", "name": "Recap Q3", "parents": ["old1"]}]}}
        if "mimeType='application/vnd.google-apps.folder'" in url:
            return {"ok": True, "json": {"files": [
                {"id": "d1", "name": "Archive"}]}}
        return {"ok": True, "json": {"files": []}}

    monkeypatch.setattr(pipedream_client, "proxy_request", fake_proxy)
    m, u, b, _ = pipedream_executor._build_drive_share(
        "o", "a", {"file": "Recap Q3", "email": "anant@sff.com", "role": "writer"})
    assert m == "POST" and "/files/f1/permissions" in u
    assert b["role"] == "writer" and b["emailAddress"] == "anant@sff.com"
    with pytest.raises(ValueError):
        pipedream_executor._build_drive_share(
            "o", "a", {"file": "Recap Q3", "email": "not-an-email"})
    m2, u2, _b2, _ = pipedream_executor._build_drive_move(
        "o", "a", {"file": "Recap Q3", "folder": "Archive"})
    assert m2 == "PATCH" and "addParents=d1" in u2 and "removeParents=old1" in u2


def test_drive_routes_stay_pipedream(monkeypatch):
    _enable_pd(monkeypatch)
    # Drive has NO native plane: connected → pipedream; NOT connected → still
    # pipedream (fails clean at execution), never a silent wrong route.
    monkeypatch.setattr(pipedream_executor, "app_connected", lambda o, a: False)
    assert executor.route_for_typed(
        {"type": "drive.create_doc", "args": {"name": "x"}}, "org1") == "pipedream"
