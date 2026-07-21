"""Pipedream Connect alternative-connections tab — flag gating, auth, and the
client's token/URL/distillation logic. Key-free: no real Pipedream calls (the
client is monkeypatched / httpx is stubbed)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, pipedream_client, store
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    return TestClient(main_module.app)


def _login(client: TestClient, email: str = "owner@x.com") -> dict:
    user = store.upsert_user(email)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "pipedream_project_id", "proj_test")
    monkeypatch.setattr(settings, "pipedream_client_id", "cid_test")
    monkeypatch.setattr(settings, "pipedream_client_secret", "csecret_test")
    monkeypatch.setattr(settings, "pipedream_environment", "development")


# ── flag OFF: the key-free demo must never see the surface ──────────────────

def test_all_routes_404_when_disabled(client):
    assert not pipedream_client.enabled()
    assert client.get("/dashboard/pipedream/accounts").status_code == 404
    assert client.get("/dashboard/pipedream/connect?app=slack").status_code == 404
    assert client.post("/dashboard/pipedream/run", json={}).status_code == 404


# ── flag ON: login gate ─────────────────────────────────────────────────────

def test_accounts_requires_login(client, monkeypatch):
    _enable(monkeypatch)
    # No cookie ⇒ the dashboard twin refuses (login required), even flag-on.
    r = client.get("/dashboard/pipedream/accounts")
    assert r.status_code == 401


def test_accounts_lists_catalog_with_connected_flags(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)
    monkeypatch.setattr(
        pipedream_client, "list_accounts",
        lambda org, app="": [{"id": "apn_1", "app": "slack",
                              "name": "Acme", "healthy": True}],
    )
    r = client.get("/dashboard/pipedream/accounts")
    assert r.status_code == 200
    body = r.json()
    assert body["environment"] == "development"
    apps = {a["slug"]: a for a in body["apps"]}
    assert apps["slack"]["connected"] is True
    assert apps["slack"]["account"]["id"] == "apn_1"
    assert apps["asana"]["connected"] is False
    # No raw credential/token field ever surfaces.
    assert "access_token" not in r.text and "authProvisionId" not in body


def test_accounts_degrades_softly_when_api_fails(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)

    def boom(org, app=""):
        raise pipedream_client.PipedreamError("down")

    monkeypatch.setattr(pipedream_client, "list_accounts", boom)
    r = client.get("/dashboard/pipedream/accounts")
    assert r.status_code == 200  # still renders the catalog
    body = r.json()
    assert body["degraded"] == "PipedreamError"
    assert all(a["connected"] is False for a in body["apps"])


# ── connect: redirect flow ──────────────────────────────────────────────────

def test_connect_redirects_to_pipedream(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)
    captured = {}

    def fake_token(org, app="", success_redirect_uri="", error_redirect_uri="",
                   allowed_origins=None):
        captured.update(org=org, app=app, success=success_redirect_uri,
                        error=error_redirect_uri)
        return {"token": "ctok_x", "connect_url": "https://pipedream.com/_static/connect.html?token=ctok_x&connectLink=true&app=slack"}

    monkeypatch.setattr(pipedream_client, "create_connect_token", fake_token)
    r = client.get("/dashboard/pipedream/connect?app=slack", follow_redirects=False)
    assert r.status_code == 303
    assert "pipedream.com/_static/connect.html" in r.headers["location"]
    assert captured["app"] == "slack"
    assert "pd=connected" in captured["success"] and "pd=error" in captured["error"]


def test_connect_rejects_malformed_slug(client, monkeypatch):
    # Any well-formed Pipedream slug is now connectable (the generic grid);
    # only a malformed slug (spaces, path chars) is refused before the redirect.
    _enable(monkeypatch)
    _login(client)
    r = client.get("/dashboard/pipedream/connect", params={"app": "bad slug!!"},
                   follow_redirects=False)
    assert r.status_code == 400
    r2 = client.get("/dashboard/pipedream/connect", params={"app": "../evil"},
                    follow_redirects=False)
    assert r2.status_code == 400


def test_connect_token_failure_redirects_to_error(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)

    def boom(org, **k):
        raise pipedream_client.PipedreamError("token down")

    monkeypatch.setattr(pipedream_client, "create_connect_token", boom)
    r = client.get("/dashboard/pipedream/connect?app=slack", follow_redirects=False)
    assert r.status_code == 303
    assert "pd=error" in r.headers["location"]


# ── run: same-origin + validation ───────────────────────────────────────────

def test_run_blocks_cross_origin(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)
    # A cross-origin browser POST (CSRF) is refused by the same-origin gate,
    # before run_action is ever reached.
    r = client.post("/dashboard/pipedream/run",
                    json={"action_id": "slack-send", "configured_props": {}},
                    headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_run_validates_and_invokes(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)
    seen = {}

    def fake_run(org, action_id, props):
        seen.update(org=org, action_id=action_id, props=props)
        return {"exports": {"$summary": "ok"}, "os": [], "ret": [1]}

    monkeypatch.setattr(pipedream_client, "run_action", fake_run)
    hdr = {"sec-fetch-site": "same-origin"}
    # missing action_id → 400
    bad = client.post("/dashboard/pipedream/run",
                      json={"configured_props": {}}, headers=hdr)
    assert bad.status_code == 400
    ok = client.post(
        "/dashboard/pipedream/run",
        json={"action_id": "slack-send-message",
              "configured_props": {"slack": {"authProvisionId": "apn_1"}}},
        headers=hdr,
    )
    assert ok.status_code == 200
    assert ok.json()["exports"]["$summary"] == "ok"
    assert seen["action_id"] == "slack-send-message"


# ── client unit: token cache, URL building, distillation ────────────────────

def test_enabled_needs_all_three_creds(monkeypatch):
    monkeypatch.setattr(settings, "pipedream_project_id", "proj")
    monkeypatch.setattr(settings, "pipedream_client_id", "cid")
    monkeypatch.setattr(settings, "pipedream_client_secret", "")
    assert pipedream_client.enabled() is False
    monkeypatch.setattr(settings, "pipedream_client_secret", "sec")
    assert pipedream_client.enabled() is True


def test_environment_defaults_safely(monkeypatch):
    monkeypatch.setattr(settings, "pipedream_environment", "garbage")
    assert pipedream_client.environment() == "development"
    monkeypatch.setattr(settings, "pipedream_environment", "production")
    assert pipedream_client.environment() == "production"


def test_create_connect_token_appends_app_slug(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        pipedream_client, "_request",
        lambda m, p, **k: {"token": "ctok_x", "expires_at": "t",
                           "connect_link_url": "https://pipedream.com/connect?token=ctok_x"},
    )
    out = pipedream_client.create_connect_token("org1", app="asana")
    assert out["token"] == "ctok_x"
    assert out["connect_url"].endswith("&app=asana")


def test_list_accounts_distills_and_hides_credentials(monkeypatch):
    _enable(monkeypatch)
    raw = {"data": [
        {"id": "apn_9", "app": {"name_slug": "slack"}, "name": "Acme",
         "healthy": True, "credentials": {"oauth_access_token": "SECRET"}},
    ]}
    monkeypatch.setattr(pipedream_client, "_request", lambda m, p, **k: raw)
    out = pipedream_client.list_accounts("org1")
    assert out == [{"id": "apn_9", "app": "slack", "name": "Acme", "healthy": True}]
    assert "SECRET" not in str(out)


def test_summary_exposes_pipedream_flag_for_nav_gating(client, monkeypatch):
    """The dashboard hides its Pipedream nav tab unless the summary reports the
    feature configured — so the key-free demo/un-configured prod look unchanged."""
    _login(client)
    off = client.get("/dashboard/summary")
    assert off.status_code == 200
    assert off.json()["settings"]["pipedream_configured"] is False
    _enable(monkeypatch)
    on = client.get("/dashboard/summary")
    assert on.json()["settings"]["pipedream_configured"] is True


def test_access_token_is_cached(monkeypatch):
    _enable(monkeypatch)
    calls = {"n": 0}

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "tok_abc", "expires_in": 3600}

    def fake_post(url, **k):
        calls["n"] += 1
        return _Resp()

    import app.pipedream_client as pc
    # reset module cache
    pc._token_value = ""
    pc._token_expiry = 0.0
    monkeypatch.setattr("httpx.post", fake_post)
    assert pc._access_token() == "tok_abc"
    assert pc._access_token() == "tok_abc"  # cached — no second exchange
    assert calls["n"] == 1


# ── P0: generic catalog grid, Connect Proxy, revoke ─────────────────────────


class _PResp:
    """A minimal httpx.Response stand-in for proxy/authed-request tests."""

    def __init__(self, status=200, js=None, text="", ctype="application/json"):
        self.status_code = status
        self._js = js
        self.text = text
        self.headers = {"content-type": ctype}

    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js


def _live_token(pc):
    pc._token_value = "tok_live"
    pc._token_expiry = pc._now() + 9999


# catalog search — client unit

def test_search_apps_parses_and_paginates(monkeypatch):
    _enable(monkeypatch)
    import app.pipedream_client as pc
    raw = {"data": [
        {"name": "Notion", "name_slug": "notion", "description": "docs",
         "categories": ["Productivity"], "img_src": "i.png"},
        {"name_slug": "stripe"},
        {"nope": 1},  # no name_slug ⇒ dropped
    ], "page_info": {"total_count": 3000, "end_cursor": "CUR"}}
    monkeypatch.setattr(pc, "_authed_request",
                        lambda m, u, **k: SimpleNamespace(status_code=200, json=lambda: raw))
    out = pc.search_apps("no", limit=2)
    assert [a["slug"] for a in out["apps"]] == ["notion", "stripe"]
    assert out["apps"][0]["name"] == "Notion" and out["apps"][0]["img"] == "i.png"
    assert out["total"] == 3000
    assert out["next_cursor"] == "CUR"  # a full page may have more


def test_search_apps_short_page_has_no_cursor(monkeypatch):
    _enable(monkeypatch)
    import app.pipedream_client as pc
    raw = {"data": [{"name_slug": "notion"}],
           "page_info": {"total_count": 1, "end_cursor": "CUR"}}
    monkeypatch.setattr(pc, "_authed_request",
                        lambda m, u, **k: SimpleNamespace(status_code=200, json=lambda: raw))
    out = pc.search_apps("no", limit=30)  # 1 < 30 ⇒ last page
    assert out["next_cursor"] == ""


# Connect Proxy — client unit

def test_proxy_request_builds_b64url_and_headers(monkeypatch):
    _enable(monkeypatch)
    import base64
    import app.pipedream_client as pc
    _live_token(pc)
    captured = {}

    def fake_request(method, url, **k):
        captured.update(method=method, url=url, headers=k.get("headers"),
                        params=k.get("params"), content=k.get("content"))
        return _PResp(200, {"users": []})

    monkeypatch.setattr("httpx.request", fake_request)
    out = pc.proxy_request("org1", "apn_5", "get",
                           "https://api.notion.com/v1/users",
                           headers={"Notion-Version": "2022-06-28"})
    target = "https://api.notion.com/v1/users"
    b64 = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert "=" not in b64  # no padding
    assert f"/connect/proj_test/proxy/{b64}" in captured["url"]
    assert captured["params"] == {"external_user_id": "org1", "account_id": "apn_5"}
    assert captured["headers"]["Authorization"] == "Bearer tok_live"
    assert captured["headers"]["x-pd-environment"] == "development"
    assert captured["headers"]["x-pd-proxy-Notion-Version"] == "2022-06-28"
    assert captured["method"] == "GET"  # method passthrough (upper-cased)
    assert captured["content"] is None  # no body on a GET
    assert out == {"status": 200, "ok": True, "json": {"users": []}}


def test_proxy_request_post_body_and_proxy_content_type(monkeypatch):
    _enable(monkeypatch)
    import json as J
    import app.pipedream_client as pc
    _live_token(pc)
    captured = {}

    def fake_request(method, url, **k):
        captured.update(headers=k.get("headers"), content=k.get("content"))
        return _PResp(200, {"ok": True})

    monkeypatch.setattr("httpx.request", fake_request)
    pc.proxy_request("org", "apn", "POST", "https://app.asana.com/api/1.0/tasks",
                     json_body={"data": {"name": "x"}})
    assert captured["headers"]["x-pd-proxy-Content-Type"] == "application/json"
    assert J.loads(captured["content"]) == {"data": {"name": "x"}}


def test_proxy_request_returns_downstream_error_without_raising(monkeypatch):
    _enable(monkeypatch)
    import app.pipedream_client as pc
    _live_token(pc)
    monkeypatch.setattr("httpx.request",
                        lambda m, u, **k: _PResp(404, None, text="nope", ctype="text/plain"))
    out = pc.proxy_request("org", "apn", "GET", "https://api.x.com/thing")
    assert out["status"] == 404 and out["ok"] is False
    assert out["json"] is None and out["text"] == "nope"


# revoke — client unit

def test_delete_account_treats_404_as_success(monkeypatch):
    _enable(monkeypatch)
    import app.pipedream_client as pc
    monkeypatch.setattr(pc, "_authed_request", lambda m, u, **k: SimpleNamespace(status_code=404))
    assert pc.delete_account("apn_gone") is True
    monkeypatch.setattr(pc, "_authed_request", lambda m, u, **k: SimpleNamespace(status_code=204))
    assert pc.delete_account("apn_ok") is True

    def boom(m, u, **k):
        raise pc.PipedreamError("x")

    monkeypatch.setattr(pc, "_authed_request", boom)
    assert pc.delete_account("apn_err") is False
    assert pc.delete_account("") is False  # empty id ⇒ no call


# API routes — generic grid + disconnect

def test_apps_and_disconnect_404_when_disabled(client):
    assert client.get("/dashboard/pipedream/apps").status_code == 404
    assert client.post("/dashboard/pipedream/disconnect", json={"app": "slack"}).status_code == 404


def test_apps_requires_login(client, monkeypatch):
    _enable(monkeypatch)
    assert client.get("/dashboard/pipedream/apps").status_code == 401


def test_apps_search_returns_page(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)
    monkeypatch.setattr(
        pipedream_client, "search_apps",
        lambda q, after="": {"apps": [{"slug": "stripe", "name": "Stripe",
                                       "description": "", "categories": [], "img": ""}],
                             "next_cursor": "C2", "total": 3000},
    )
    r = client.get("/dashboard/pipedream/apps", params={"q": "stri"})
    assert r.status_code == 200
    b = r.json()
    assert b["apps"][0]["slug"] == "stripe" and b["next_cursor"] == "C2"


def test_apps_search_degrades_softly(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)

    def boom(q, after=""):
        raise pipedream_client.PipedreamError("down")

    monkeypatch.setattr(pipedream_client, "search_apps", boom)
    r = client.get("/dashboard/pipedream/apps")
    assert r.status_code == 200 and r.json()["apps"] == []


def test_connect_allows_any_valid_slug(client, monkeypatch):
    # A non-featured app (stripe ∉ _CATALOG) is connectable via the generic grid.
    _enable(monkeypatch)
    _login(client)
    monkeypatch.setattr(
        pipedream_client, "create_connect_token",
        lambda org, **k: {"token": "t",
                          "connect_url": "https://pipedream.com/_static/connect.html?token=t&app=stripe"},
    )
    r = client.get("/dashboard/pipedream/connect", params={"app": "stripe"},
                   follow_redirects=False)
    assert r.status_code == 303 and "pipedream.com" in r.headers["location"]


def test_disconnect_blocks_cross_origin(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)
    r = client.post("/dashboard/pipedream/disconnect", json={"app": "slack"},
                    headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_disconnect_only_revokes_owned_accounts(client, monkeypatch):
    # Cross-tenant guard: a caller-supplied foreign account_id is NOT deleted;
    # only accounts Pipedream reports as owned by this org are revoked.
    _enable(monkeypatch)
    _login(client)
    monkeypatch.setattr(
        pipedream_client, "list_accounts",
        lambda org, app="": [{"id": "apn_own", "app": "slack", "healthy": True}],
    )
    deleted = []
    monkeypatch.setattr(pipedream_client, "delete_account",
                        lambda aid: (deleted.append(aid) or True))
    r = client.post("/dashboard/pipedream/disconnect",
                    json={"app": "slack", "account_id": "apn_ATTACKER"},
                    headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 200
    assert deleted == ["apn_own"]  # never apn_ATTACKER
    assert r.json()["revoked"] == 1


def test_disconnect_specific_owned_account(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)
    monkeypatch.setattr(
        pipedream_client, "list_accounts",
        lambda org, app="": [{"id": "apn_a", "app": "slack"}, {"id": "apn_b", "app": "slack"}],
    )
    deleted = []
    monkeypatch.setattr(pipedream_client, "delete_account",
                        lambda aid: (deleted.append(aid) or True))
    r = client.post("/dashboard/pipedream/disconnect",
                    json={"app": "slack", "account_id": "apn_b"},
                    headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 200 and deleted == ["apn_b"]


# ── pre-built actions catalog (what a tool can do) ──────────────────────────

def test_actions_404_when_disabled(client):
    assert client.get("/dashboard/pipedream/actions?app=notion").status_code == 404


def test_actions_requires_login(client, monkeypatch):
    _enable(monkeypatch)
    assert client.get("/dashboard/pipedream/actions?app=notion").status_code == 401


def test_actions_lists_prebuilt(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)
    monkeypatch.setattr(pipedream_client, "list_actions",
                        lambda slug, limit=25: [{"key": "notion-create-page", "name": "Create Page"},
                                                {"key": "notion-append", "name": "Append Block"}])
    r = client.get("/dashboard/pipedream/actions?app=notion")
    assert r.status_code == 200
    b = r.json()
    assert b["app"] == "notion" and b["actions"][0]["name"] == "Create Page"


def test_actions_rejects_bad_slug(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)
    assert client.get("/dashboard/pipedream/actions", params={"app": "bad slug!!"}).status_code == 400
