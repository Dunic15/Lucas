"""Asana integration (docs/ASANA.md) — client, executor, producer, Petra.

Key-free: Asana is mocked at the HTTP layer inside asana_client, the store is
a fresh temp SQLite, and every executor flag is toggled per test. Covers the
four layers: the client contract (soft returns, token precedence, brief
building + cache), executor dispatch + receipts + auto-push, the typed-action
producer's grounding rules, and the Petra avatar itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import asana_client, avatars, executor, store  # noqa: E402
from app.api import oauth as oauth_mod  # noqa: E402  (oauth extracted)
from app.config import settings  # noqa: E402


def _fresh_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()


class _Resp:
    def __init__(self, code: int, payload: dict):
        self.status_code = code
        self._p = payload

    def json(self) -> dict:
        return self._p


_PROJECTS = [{"gid": "101", "name": "Onboarding"}, {"gid": "102", "name": "Website"}]
_TASKS = [
    {"gid": "t1", "name": "Ship SSO", "due_on": "2020-01-01", "completed": False,
     "assignee": {"name": "Dana"}},
    {"gid": "t2", "name": "Old thing", "due_on": "", "completed": True,
     "assignee": None},
]


def _mock_asana(monkeypatch, *, task_resp=None, org_id="org-a"):
    """asana_client with a token configured and Asana mocked; returns the
    captured request log [(method, path, body-or-params)].

    The token is seeded as the ORG'S OWN grant. It used to rely on the
    ASANA_TOKEN env fallback, but that PAT is the deployment owner's workspace
    and no longer serves an arbitrary tenant (security fix 2026-07-23) — and a
    tenant connected to its own Asana is the realistic shape anyway."""
    monkeypatch.setattr(settings, "asana_token", "pat-env")
    monkeypatch.setattr(settings, "session_secret", "sek")
    if org_id:
        store.set_org_oauth(org_id, "pat-env", provider="asana")
    monkeypatch.setattr(settings, "asana_workspace_gid", "ws-1")
    log: list[tuple] = []
    task_resp = task_resp or {
        "gid": "t-new", "name": "Ship SSO",
        "permalink_url": "https://app.asana.com/0/101/t-new/f",
    }

    def fake_get(url, *, params=None, headers=None, timeout=None):
        log.append(("GET", url, dict(params or {})))
        if "/projects" in url:
            return _Resp(200, {"data": _PROJECTS})
        if "/tasks" in url and "typeahead" not in url:
            return _Resp(200, {"data": _TASKS})
        if "typeahead" in url:
            return _Resp(200, {"data": _TASKS[:1]})
        return _Resp(404, {})

    def fake_request(method, url, *, params=None, headers=None, json=None, timeout=None):
        log.append((method, url, (json or {}).get("data") or {}))
        return _Resp(200, {"data": dict(task_resp)})

    monkeypatch.setattr(asana_client.httpx, "get", fake_get)
    monkeypatch.setattr(asana_client.httpx, "request", fake_request)
    return log


# ── client: auth + soft contract ──

def test_not_connected_is_soft(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    assert asana_client.connected("org-a") is False
    r = asana_client.create_task("org-a", {"name": "x"})
    assert r["ok"] is False and "not connected" in r["error"]
    assert asana_client.list_projects("org-a")["ok"] is False
    assert asana_client.workspace_brief("org-a") == ""


def test_org_row_beats_env_token(monkeypatch, tmp_path):
    """Precedence, and the tenancy boundary around the env PAT.

    ASANA_TOKEN is the DEPLOYMENT owner's workspace, so it serves only the
    deployment's own org; a real tenant needs its own grant (security fix
    2026-07-23 — before it, 'org-a' silently executed inside the owner's
    Asana)."""
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "session_secret", "sek")
    monkeypatch.setattr(settings, "asana_token", "pat-env")
    assert asana_client._token(str(settings.demo_org_id)) == ("pat-env", "")
    token, err = asana_client._token("org-a")
    assert token == "" and "not connected" in err
    store.set_org_oauth("org-a", "pat-org", provider="asana")
    assert asana_client._token("org-a") == ("pat-org", "")


def test_list_projects_and_project_resolution(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    _mock_asana(monkeypatch)
    listing = asana_client.list_projects("org-a")
    assert listing["ok"] and listing["projects"][0]["name"] == "Onboarding"
    assert asana_client._resolve_project("org-a", "101") == ("101", "")
    assert asana_client._resolve_project("org-a", "onboarding") == ("101", "")
    gid, err = asana_client._resolve_project("org-a", "No Such Board")
    assert gid == "" and "no Asana project" in err


# ── client: workspace brief ──

def test_workspace_brief_content_and_cache(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    log = _mock_asana(monkeypatch)
    brief = asana_client.workspace_brief("org-a")
    assert "Onboarding" in brief and "Ship SSO" in brief
    assert "OVERDUE" in brief  # due 2020-01-01 is long past
    assert "Old thing" not in brief  # completed tasks stay out
    n = len(log)
    assert asana_client.workspace_brief("org-a") == brief
    assert len(log) == n  # second read served from the TTL cache


# ── client: writes ──

def test_create_task_body_and_receipt(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    log = _mock_asana(monkeypatch)
    r = asana_client.create_task(
        "org-a",
        {"name": "Ship SSO", "project": "Onboarding", "assignee": "dana@acme.com",
         "due_on": "2026-08-01", "notes": "from the status review"},
    )
    assert r["ok"] and r["task_url"].endswith("/t-new/f")
    method, url, body = log[-1]
    assert method == "POST" and url.endswith("/tasks")
    assert body["name"] == "Ship SSO" and body["workspace"] == "ws-1"
    assert body["projects"] == ["101"]  # name resolved to the gid
    assert body["assignee"] == "dana@acme.com" and body["due_on"] == "2026-08-01"


def test_update_and_comment_validation(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    _mock_asana(monkeypatch)
    assert asana_client.update_task("org-a", {"completed": True})["ok"] is False
    assert asana_client.update_task("org-a", {"task": "t1x", "completed": True})["ok"] is False
    assert asana_client.update_task("org-a", {"task": "123", "completed": True})["ok"] is True
    assert asana_client.add_comment("org-a", {"task": "123"})["ok"] is False
    assert asana_client.add_comment("org-a", {"task": "123", "text": "hi"})["ok"] is True


# ── executor: bridge, family, dispatch, auto-push ──

def test_from_typed_and_capability_family():
    act = executor.from_typed({"type": "asana.create_task", "args": {"name": "x"}})
    assert act == {"type": "asana.create_task", "task": {"name": "x"}}
    assert executor.from_typed({"type": "unknown.thing", "args": {}}) is None
    assert executor.capability_family("asana.create_task") == "asana"
    assert executor.capability_family("email.send") == "google"
    assert executor.capability_family(None) == "google"


def test_execute_approved_asana_writes_done_receipt(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "native_executor", True)
    statuses: list[tuple] = []
    monkeypatch.setattr(
        executor.ledger, "set_action_status",
        lambda aid, status, detail="", *, org_id="", receipt=None: statuses.append(
            (aid, status, detail, org_id)
        ),
    )
    monkeypatch.setattr(
        executor.asana_client, "create_task",
        lambda org, task: {"ok": True, "task_gid": "t9",
                           "task_url": "https://app.asana.com/0/0/t9/f"},
    )
    r = executor.execute_approved(
        "org-a", "a1", {"type": "asana.create_task", "task": {"name": "x"}}
    )
    assert r["ok"]
    assert statuses[-1][:2] == ("a1", "done")
    assert "t9" in statuses[-1][2] and statuses[-1][3] == "org-a"


def test_auto_execute_asana_gated_and_selective(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(
        executor.asana_client, "create_task",
        lambda org, task: calls.append(task) or {"ok": True, "task_gid": "t9",
                                                 "task_url": "u"},
    )
    actions = [
        {"item": "a", "action_id": "a1",
         "typed": {"type": "asana.create_task", "args": {"name": "a"}}},
        {"item": "b", "action_id": "a2",
         "typed": {"type": "email.send", "args": {"to": ["x@y.z"], "subject": "s"}}},
        {"item": "c",  # no action_id → skipped
         "typed": {"type": "asana.create_task", "args": {"name": "c"}}},
        {"item": "d", "action_id": "a4"},  # untyped → skipped
    ]
    # Flag off (default): nothing runs even with the executor on.
    monkeypatch.setattr(settings, "native_executor", True)
    assert executor.auto_execute_asana("org-a", actions) == 0
    # Flag on: only the typed asana action with an id runs; email never does.
    monkeypatch.setattr(settings, "asana_auto_execute", True)
    assert executor.auto_execute_asana("org-a", actions) == 1
    assert len(calls) == 1 and calls[0]["name"] == "a"


# ── typed-action producer: grounding ──

def test_stub_producer_types_asana_only_on_an_explicit_cue():
    """Owner rule 2026-07-24 ("Asana solo quando lo dice"): a leftover work
    item WITHOUT a task/Asana cue stays untyped even with allow_asana — the
    old catch-all filed every leftover onto the team's real board."""
    from app.brain.engine import type_actions

    actions = [
        {"item": "Update the roadmap deck before Friday", "action_id": "x1"},
        {"item": "Email the recap to dana@acme.com", "action_id": "x2"},
        {"item": "Create an Asana task for the onboarding checklist",
         "action_id": "x3"},
    ]
    plain = type_actions(actions, provider="stub")
    assert "typed" not in plain[0]  # no asana without the flag
    typed = type_actions(actions, provider="stub", allow_asana=True)
    assert "typed" not in typed[0]  # no cue → stays untyped (NOT asana)
    assert typed[1]["typed"]["type"] == "email.send"  # email intent still wins
    assert typed[2]["typed"]["type"] == "asana.create_task"  # explicit cue


def test_sanitize_asana_grounding_rules():
    from app.brain.engine import _sanitize_typed

    action = {"item": "Fix the login bug", "owner": "", "deadline": "2026-08-01"}
    spec = _sanitize_typed(
        {"type": "asana.create_task",
         "args": {"name": "Fix the login bug", "due_on": "2026-08-01",
                  "assignee": "ghost@nowhere.com", "project": "Skunkworks"}},
        action, brief="we discussed the Website project",
    )
    assert spec["args"]["name"] == "Fix the login bug"
    assert spec["args"]["due_on"] == "2026-08-01"  # ISO-shaped → kept
    assert "assignee" not in spec["args"]  # email not in source → dropped
    assert "project" not in spec["args"]  # name not in source → dropped

    spec2 = _sanitize_typed(
        {"type": "asana.create_task",
         "args": {"name": "Fix the login bug", "project": "Website",
                  "due_on": "next Friday"}},
        action, brief="we discussed the Website project",
    )
    assert spec2["args"]["project"] == "Website"  # appears in brief → kept
    assert "due_on" not in spec2["args"]  # not ISO-shaped → dropped


# ── Petra + eligibility ──

def test_petra_avatar_loads_with_knowledge_and_template():
    petra = avatars.load("petra")
    # Presented name is "Laura" (owner rename 2026-07-26); the id stays petra.
    assert petra.name == "Laura" and "laura" in petra.wake_words
    docs = list(petra.knowledge_dir.glob("*.md"))
    assert len(docs) >= 5
    assert (petra.dir / "process_templates" / "project_status_review.yaml").exists()


def test_avatar_asana_enabled_rules(monkeypatch, tmp_path):
    import app.main as main_module

    _fresh_store(monkeypatch, tmp_path)
    # Not connected → never eligible.
    assert main_module._avatar_asana_enabled("org-a", "petra") is False
    # Connected (the org's OWN grant — the env PAT is the deployment owner's
    # and no longer serves a tenant) → default ON for any avatar…
    monkeypatch.setattr(settings, "session_secret", "sek")
    store.set_org_oauth("org-a", "pat-org", provider="asana")
    assert main_module._avatar_asana_enabled("org-a", "petra") is True
    # …until the per-avatar toggle is explicitly switched off.
    store.set_avatar_capability("petra", "asana", False)
    assert main_module._avatar_asana_enabled("org-a", "petra") is False


# ── dashboard connect/disconnect (the Connections card) ──

def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import app.main as main_module

    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "session_secret", "sek")
    return TestClient(main_module.app)


def _login(client) -> dict:
    from app import auth

    user = store.upsert_user("owner@x.com")
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def test_connect_asana_requires_login(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    r = client.post("/dashboard/connections/asana", json={"token": "pat-x"})
    assert r.status_code == 401


def test_connect_asana_verifies_stores_and_disconnects(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    user = _login(client)
    monkeypatch.setattr(
        asana_client, "verify_token",
        lambda pat: {"ok": True, "email": "pm@acme.com",
                     "workspace": "Acme HQ", "workspace_gid": "ws-9"},
    )

    r = client.post("/dashboard/connections/asana", json={"token": "pat-real"})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True and body["workspace"] == "Acme HQ"
    assert "pat-real" not in r.text  # the token is never echoed back
    row = store.get_org_oauth(user["org_id"], provider="asana")
    assert row["refresh_token"] == "pat-real" and row["scopes"] == "ws-9"
    assert asana_client.connected(user["org_id"]) is True

    r2 = client.post("/dashboard/connections/asana/disconnect")
    assert r2.status_code == 200 and r2.json()["removed"] is True
    assert store.get_org_oauth(user["org_id"], provider="asana") is None
    assert asana_client.connected(user["org_id"]) is False


def test_connect_asana_bad_token_stores_nothing(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    user = _login(client)
    monkeypatch.setattr(
        asana_client, "verify_token",
        lambda pat: {"ok": False, "error": "asana rejected the token (HTTP 401)"},
    )
    r = client.post("/dashboard/connections/asana", json={"token": "typo"})
    assert r.status_code == 400 and "rejected" in r.json()["error"]
    assert store.get_org_oauth(user["org_id"], provider="asana") is None

    r2 = client.post("/dashboard/connections/asana", json={})
    assert r2.status_code == 400  # missing token is a clean client error


# ── OAuth: the one-click "Connect Asana" button ──

def _oauth_app(monkeypatch):
    monkeypatch.setattr(settings, "asana_client_id", "cid-asana")
    monkeypatch.setattr(settings, "asana_client_secret", "csec-asana")
    monkeypatch.setattr(settings, "public_base_url", "https://laura.example")


def test_oauth_connect_redirects_to_asana_with_state(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _oauth_app(monkeypatch)
    r = client.get("/oauth/asana/connect", follow_redirects=False)
    assert r.status_code in (302, 307)
    loc = r.headers["location"]
    assert loc.startswith("https://app.asana.com/-/oauth_authorize")
    assert "client_id=cid-asana" in loc and "state=" in loc
    assert "redirect_uri=https%3A%2F%2Flaura.example%2Foauth%2Fasana%2Fcallback" in loc
    assert "laura_asana_oauth_state" in r.headers.get("set-cookie", "")


def test_oauth_connect_400_without_app(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    r = client.get("/oauth/asana/connect", follow_redirects=False)
    assert r.status_code == 400  # no ASANA_CLIENT_ID → clear config error


def test_oauth_callback_stores_grant_and_lands_connected(monkeypatch, tmp_path):
    import app.main as main_module
    from app import auth

    client = _client(monkeypatch, tmp_path)
    _oauth_app(monkeypatch)
    monkeypatch.setattr(
        asana_client, "exchange_code",
        lambda code, uri: {"ok": True, "access_token": "at-1",
                           "refresh_token": "rt-oauth", "email": "pm@acme.com",
                           "name": "PM"},
    )
    monkeypatch.setattr(
        asana_client, "verify_token",
        lambda tok: {"ok": True, "email": "pm@acme.com",
                     "workspace": "Acme HQ", "workspace_gid": "ws-9"},
    )
    nonce, signed = auth.issue_oauth_state(oauth_mod.ASANA_STATE_PURPOSE)
    client.cookies.set(oauth_mod.ASANA_STATE_COOKIE, nonce)

    r = client.get(
        f"/oauth/asana/callback?code=c-1&state={signed}", follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard?asana=connected"
    row = store.get_org_oauth(settings.demo_org_id, provider="asana-oauth")
    assert row["refresh_token"] == "rt-oauth" and row["scopes"] == "ws-9"


def test_oauth_callback_bad_state_stores_nothing(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _oauth_app(monkeypatch)
    r = client.get(
        "/oauth/asana/callback?code=c-1&state=forged.sig", follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard?asana=error"
    assert store.get_org_oauth(settings.demo_org_id, provider="asana-oauth") is None


def test_token_precedence_oauth_grant_over_pat(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "session_secret", "sek")
    _oauth_app(monkeypatch)
    store.set_org_oauth("org-a", "pat-1", provider="asana")
    store.set_org_oauth("org-a", "rt-oauth", provider="asana-oauth")
    posts = {"n": 0}

    def fake_post(url, *, data=None, timeout=None):
        posts["n"] += 1
        assert data["grant_type"] == "refresh_token"
        return _Resp(200, {"access_token": "at-oauth", "expires_in": 3600})

    monkeypatch.setattr(asana_client.httpx, "post", fake_post)
    assert asana_client._token("org-a") == ("at-oauth", "")  # grant wins
    assert asana_client._token("org-a") == ("at-oauth", "")  # cached
    assert posts["n"] == 1


def test_broken_oauth_grant_falls_through_to_pat(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "session_secret", "sek")
    _oauth_app(monkeypatch)
    store.set_org_oauth("org-a", "pat-1", provider="asana")
    store.set_org_oauth("org-a", "rt-dead", provider="asana-oauth")
    monkeypatch.setattr(
        asana_client.httpx, "post",
        lambda url, *, data=None, timeout=None: _Resp(400, {}),
    )
    assert asana_client._token("org-a") == ("pat-1", "")  # revoked grant ≠ outage


def test_verify_token_reads_user_and_workspace(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)

    def fake_get(url, *, params=None, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer pat-x"
        return _Resp(200, {"data": {"email": "pm@acme.com", "workspaces": [
            {"gid": "ws-9", "name": "Acme HQ"}]}})

    monkeypatch.setattr(asana_client.httpx, "get", fake_get)
    info = asana_client.verify_token("pat-x")
    assert info == {"ok": True, "email": "pm@acme.com",
                    "workspace": "Acme HQ", "workspace_gid": "ws-9"}
    assert asana_client.verify_token("")["ok"] is False
