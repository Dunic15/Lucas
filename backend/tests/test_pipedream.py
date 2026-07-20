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


def test_connect_unknown_app_is_400(client, monkeypatch):
    _enable(monkeypatch)
    _login(client)
    r = client.get("/dashboard/pipedream/connect?app=notanapp",
                   follow_redirects=False)
    assert r.status_code == 400


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
