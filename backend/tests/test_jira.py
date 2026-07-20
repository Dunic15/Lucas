"""Jira connector (jira_client) — the self-serve Connections-tab integration.

Key-free: Jira is mocked at the HTTP layer inside jira_client, the store is a
fresh temp SQLite. Covers the client contract (soft returns, cred precedence,
brief building + cache) and the dashboard connect/disconnect route (verify →
store encrypted per-org → the token is never echoed).
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import jira_client, store  # noqa: E402
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


_ISSUES = {
    "issues": [
        {"key": "ENG-1", "fields": {"summary": "Ship SSO",
         "assignee": {"displayName": "Dana"}, "duedate": "2026-08-01",
         "status": {"name": "In Progress"}}},
        {"key": "ENG-2", "fields": {"summary": "Fix login",
         "assignee": None, "duedate": None, "status": {"name": "To Do"}}},
    ]
}
_PROJECTS = {"values": [{"key": "ENG", "name": "Engineering"},
                        {"key": "MKT", "name": "Marketing"}]}


def _mock_jira(monkeypatch):
    """jira_client with env creds configured and Jira mocked; returns the
    captured GET log [(url, params, auth_header)]."""
    monkeypatch.setattr(settings, "jira_site", "https://acme.atlassian.net")
    monkeypatch.setattr(settings, "jira_email", "pm@acme.com")
    monkeypatch.setattr(settings, "jira_api_token", "tok-env")
    log: list[tuple] = []

    def fake_get(url, *, params=None, headers=None, timeout=None):
        log.append((url, dict(params or {}), (headers or {}).get("Authorization", "")))
        if url.endswith("/myself"):
            return _Resp(200, {"emailAddress": "pm@acme.com", "displayName": "PM"})
        if "/project/search" in url:
            return _Resp(200, _PROJECTS)
        if "/search" in url:
            return _Resp(200, _ISSUES)
        return _Resp(404, {})

    monkeypatch.setattr(jira_client.httpx, "get", fake_get)
    return log


# ── client contract ──
def test_not_connected_is_soft(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "jira_site", "")
    monkeypatch.setattr(settings, "jira_email", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    assert jira_client.connected("org-a") is False
    r = jira_client.list_projects("org-a")
    assert r["ok"] is False and "not connected" in r["error"]


def test_org_row_beats_env(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    _mock_jira(monkeypatch)
    monkeypatch.setattr(settings, "session_secret", "sek")
    log = _mock_jira(monkeypatch)
    store.set_org_oauth("org-a", "tok-org", provider="jira",
                        email="me@acme.com", scopes="https://acme.atlassian.net")
    jira_client._reset_brief_cache()
    assert jira_client.connected("org-a") is True
    # A read uses the ORG token (Basic me@acme:tok-org), not the env creds.
    jira_client.list_projects("org-a")
    expected = "Basic " + base64.b64encode(b"me@acme.com:tok-org").decode()
    assert log[-1][2] == expected


def test_verify_token_shapes(monkeypatch, tmp_path):
    _mock_jira(monkeypatch)
    ok = jira_client.verify_token("https://acme.atlassian.net", "pm@acme.com", "tok")
    assert ok["ok"] is True and ok["email"] == "pm@acme.com"
    assert jira_client.verify_token("", "e", "t")["ok"] is False
    assert jira_client.verify_token("http://insecure", "e", "t")["ok"] is False  # not https


def test_workspace_brief_content_and_cache(monkeypatch, tmp_path):
    log = _mock_jira(monkeypatch)
    jira_client._reset_brief_cache()
    brief = jira_client.workspace_brief("org-a")
    assert "ENG-1" in brief and "Ship SSO" in brief and "Dana" in brief
    assert "due: 2026-08-01" in brief
    hits_after_first = len(log)
    jira_client.workspace_brief("org-a")  # cached — no new HTTP
    assert len(log) == hits_after_first


def test_list_projects(monkeypatch, tmp_path):
    _mock_jira(monkeypatch)
    r = jira_client.list_projects("org-a")
    assert r["ok"] is True
    assert {p["key"] for p in r["projects"]} == {"ENG", "MKT"}


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


def test_connect_jira_requires_login(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    r = client.post("/dashboard/connections/jira",
                    json={"site": "https://x.atlassian.net", "email": "a@b.c",
                          "token": "t"})
    assert r.status_code == 401


def test_connect_jira_verifies_stores_and_disconnects(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    user = _login(client)
    monkeypatch.setattr(
        jira_client, "verify_token",
        lambda site, email, token: {"ok": True, "email": "pm@acme.com",
                                    "name": "PM", "site": "https://acme.atlassian.net"},
    )
    r = client.post("/dashboard/connections/jira",
                    json={"site": "https://acme.atlassian.net",
                          "email": "pm@acme.com", "token": "tok-real"})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True and body["site"] == "https://acme.atlassian.net"
    assert "tok-real" not in r.text  # token never echoed
    row = store.get_org_oauth(user["org_id"], provider="jira")
    assert row["refresh_token"] == "tok-real"
    assert row["scopes"] == "https://acme.atlassian.net"
    assert jira_client.connected(user["org_id"]) is True

    r2 = client.post("/dashboard/connections/jira/disconnect")
    assert r2.status_code == 200 and r2.json()["removed"] is True
    assert store.get_org_oauth(user["org_id"], provider="jira") is None


def test_connect_jira_bad_creds_store_nothing(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    user = _login(client)
    monkeypatch.setattr(
        jira_client, "verify_token",
        lambda site, email, token: {"ok": False, "error": "Jira rejected the credentials (401)"},
    )
    r = client.post("/dashboard/connections/jira",
                    json={"site": "https://acme.atlassian.net", "email": "x@y.z",
                          "token": "typo"})
    assert r.status_code == 400 and "rejected" in r.json()["error"]
    assert store.get_org_oauth(user["org_id"], provider="jira") is None


# ── OAuth (Atlassian 3LO) redirect flow ──
def test_exchange_code_resolves_cloudid(monkeypatch):
    monkeypatch.setattr(settings, "jira_client_id", "cid")
    monkeypatch.setattr(settings, "jira_client_secret", "sec")

    def fake_post(url, *, json=None, timeout=None):
        return _Resp(200, {"access_token": "acc", "refresh_token": "ref"})

    def fake_get(url, *, headers=None, timeout=None, params=None):
        # accessible-resources → the org's Jira site + cloud id
        return _Resp(200, [{"id": "cloud-123", "url": "https://acme.atlassian.net"}])

    monkeypatch.setattr(jira_client.httpx, "post", fake_post)
    monkeypatch.setattr(jira_client.httpx, "get", fake_get)
    out = jira_client.exchange_code("the-code", "https://laura/oauth/jira/callback")
    assert out["ok"] is True and out["refresh_token"] == "ref"
    assert out["cloudid"] == "cloud-123" and out["site"] == "https://acme.atlassian.net"


def test_oauth_grant_beats_token_and_targets_cloud_api(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "session_secret", "sek")
    monkeypatch.setattr(settings, "jira_client_id", "cid")
    monkeypatch.setattr(settings, "jira_client_secret", "sec")
    log: list[tuple] = []

    def fake_post(url, *, json=None, timeout=None):  # refresh_token → access token
        return _Resp(200, {"access_token": "acc-tok", "expires_in": 3600})

    def fake_get(url, *, headers=None, timeout=None, params=None):
        log.append((url, (headers or {}).get("Authorization", "")))
        return _Resp(200, _PROJECTS)

    monkeypatch.setattr(jira_client.httpx, "post", fake_post)
    monkeypatch.setattr(jira_client.httpx, "get", fake_get)
    store.set_org_oauth("org-o", "refresh-1", provider="jira-oauth",
                        email="https://acme.atlassian.net", scopes="cloud-123")
    jira_client._reset_brief_cache()
    assert jira_client.connected("org-o") is True
    jira_client.list_projects("org-o")
    # Reads target the Atlassian cloud API with a Bearer token, not the site.
    assert "api.atlassian.com/ex/jira/cloud-123" in log[-1][0]
    assert log[-1][1] == "Bearer acc-tok"


def test_oauth_connect_route_needs_app_config(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _login(client)
    monkeypatch.setattr(settings, "jira_client_id", "")
    monkeypatch.setattr(settings, "jira_client_secret", "")
    r = client.get("/oauth/jira/connect", follow_redirects=False)
    assert r.status_code == 400  # no Atlassian app → honest error, no redirect

    monkeypatch.setattr(settings, "jira_client_id", "cid")
    monkeypatch.setattr(settings, "jira_client_secret", "sec")
    r2 = client.get("/oauth/jira/connect", follow_redirects=False)
    assert r2.status_code in (302, 307)
    assert "auth.atlassian.com/authorize" in r2.headers["location"]
