"""Connect the brain — the org→Slack-workspace link behind the Configure tab.
POST saves the wiring and provisions on Cedric when configured (connected) or
records it locally when not (pending); DELETE marks disconnected; the summary
exposes org_connections for the logged-in org only. Key-free."""
from __future__ import annotations

import base64
import importlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, cedric, ledger, store
from app.cedric import callback, install_state, secret_registry
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    monkeypatch.setattr(settings, "session_secret", "test-secret")
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csec")
    return TestClient(main_module.app)


def _login(client) -> dict:
    """Mint a session cookie exactly like the OAuth callback does."""
    user = store.upsert_user("owner@example.com", "Owner")
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def test_connect_requires_login(client):
    r = client.post(
        "/dashboard/connections/brain",
        json={"avatar_id": "cedric", "team_id": "T123"},
    )
    assert r.status_code == 401


def test_connect_pending_without_orgs_url(client, monkeypatch):
    monkeypatch.setattr(settings, "cedric_orgs_url", "")
    user = _login(client)
    r = client.post(
        "/dashboard/connections/brain",
        json={"avatar_id": "cedric", "team_id": "T0123ABCD", "channel": "#growth"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pending"  # saved, awaiting Cedric's endpoint

    rows = store.connections_for_org(user["org_id"])
    assert rows and rows[0]["provider"] == "cedric-brain"
    assert rows[0]["config"]["team_id"] == "T0123ABCD"

    # the summary exposes it to the owner
    data = client.get("/dashboard/summary").json()
    assert data["org_connections"][0]["status"] == "pending"


def test_connect_connected_when_provisioning_succeeds(client, monkeypatch):
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    calls: list[tuple] = []

    def fake_provision(org_id, team_id, channel="", avatar_id=""):
        calls.append((org_id, team_id, channel, avatar_id))
        return callback.ProvisionResult(200, "minted-test-secret")

    # dashboard.py calls the package re-export, so patch that binding
    monkeypatch.setattr(cedric, "provision_org", fake_provision)
    registry_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        secret_registry,
        "upsert_org_secret",
        lambda org, secret: registry_calls.append((org, secret)) or True,
    )
    user = _login(client)
    r = client.post(
        "/dashboard/connections/brain",
        json={"avatar_id": "cedric", "team_id": "T9", "channel": "#ops"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "connected"
    assert r.json()["registry_synced"] is True
    assert calls == [(user["org_id"], "T9", "#ops", "cedric")]
    assert registry_calls == [(user["org_id"], "minted-test-secret")]


def test_connect_stays_pending_when_registry_write_fails(client, monkeypatch):
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    monkeypatch.setattr(
        cedric, "provision_org", lambda *a, **k: callback.ProvisionResult(200, "minted")
    )
    monkeypatch.setattr(secret_registry, "upsert_org_secret", lambda *a: False)
    _login(client)
    r = client.post(
        "/dashboard/connections/brain",
        json={"avatar_id": "cedric", "team_id": "T9", "channel": "#ops"},
    )
    assert r.status_code == 200
    assert r.json()["provisioned"] is True
    assert r.json()["registry_synced"] is False
    assert r.json()["status"] == "pending"


def test_add_to_slack_start_carries_signed_org_state(client, monkeypatch):
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    monkeypatch.setattr(settings, "cedric_orgs_token", "shared-test-token")
    monkeypatch.setattr(settings, "public_base_url", "https://laura.example")
    user = _login(client)

    response = client.get(
        "/dashboard/connections/brain/slack/start",
        params={"avatar_id": "cedric", "channel": "#approvals"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    target = urlsplit(response.headers["location"])
    assert (target.scheme, target.netloc, target.path) == (
        "https", "cedric", "/api/slack/install"
    )
    state = parse_qs(target.query)["state"][0]
    payload = state.rsplit(".", 1)[0]
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert data["org_id"] == user["org_id"]
    assert data["avatar_id"] == "cedric"
    assert data["channel"] == "#approvals"
    assert data["return_url"] == "https://laura.example/dashboard"


def test_slack_complete_hot_writes_registry_and_connection(client, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "machine-token")
    user = store.upsert_user("owner@example.com", "Owner")
    # PR D binding rule: /complete only binds an org that INITIATED an install
    # (signed state echo or a pending row from slack/start / connect).
    store.set_connection(user["org_id"], "cedric", "cedric-brain", "pending", {})
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        secret_registry,
        "upsert_org_credentials",
        lambda org, secret, token: writes.append((org, secret, token)) or True,
    )

    state = install_state.pack(user["org_id"], "cedric", "#approvals", "")

    response = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers={"Authorization": "Bearer machine-token"},
        json={
            "org_id": user["org_id"],
            "avatar_id": "cedric",
            "team_id": "T_NEW",
            "channel": "#approvals",
            "webhook_secret": "minted-test-secret",
            "webhook_token": "minted-peer-token",
            "state": state,
        },
    )

    assert response.status_code == 200
    assert writes == [
        (user["org_id"], "minted-test-secret", "minted-peer-token")
    ]
    row = store.connections_for_org(user["org_id"])[0]
    assert row["status"] == "connected"
    assert row["config"] == {"team_id": "T_NEW", "channel": "#approvals"}
    # the per-workspace bearer is returned ONCE and resolves to this org
    token = response.json()["org_token"]
    assert token and store.resolve_org_token(token) == user["org_id"]


def test_dashboard_uses_add_to_slack_not_team_id_field():
    dashboard = (Path(__file__).resolve().parents[2] / "frontend/dashboard.html").read_text()
    assert "Add to Slack" in dashboard
    assert "Slack team ID" not in dashboard


def test_connect_validates_avatar_and_team(client):
    _login(client)
    r = client.post("/dashboard/connections/brain", json={"avatar_id": "ghost", "team_id": "T1"})
    assert r.status_code == 400
    r = client.post("/dashboard/connections/brain", json={"avatar_id": "cedric", "team_id": ""})
    assert r.status_code == 400


def test_disconnect(client, monkeypatch):
    monkeypatch.setattr(settings, "cedric_orgs_url", "")
    user = _login(client)
    client.post(
        "/dashboard/connections/brain",
        json={"avatar_id": "cedric", "team_id": "T1"},
    )
    r = client.delete("/dashboard/connections/brain/cedric")
    assert r.status_code == 200 and r.json()["status"] == "disconnected"
    rows = store.connections_for_org(user["org_id"])
    assert rows[0]["status"] == "disconnected"


def test_org_connections_scoped_to_owner(client, monkeypatch):
    monkeypatch.setattr(settings, "cedric_orgs_url", "")
    _login(client)
    client.post(
        "/dashboard/connections/brain",
        json={"avatar_id": "cedric", "team_id": "T1"},
    )
    # a different user sees an empty list, not the first org's wiring
    other = store.upsert_user("other@example.com", "Other")
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(other["user_id"]))
    data = client.get("/dashboard/summary").json()
    assert data["org_connections"] == []


def test_provision_org_unconfigured_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "cedric_orgs_url", "")
    assert callback.provision_org("org1", "T1") is None


def test_first_login_preprovision_uses_pending_route(monkeypatch):
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    monkeypatch.setattr(settings, "laura_api_token", "machine-token")
    calls: list[tuple[str, dict]] = []

    class FakeResponse:
        status_code = 202
        headers: dict = {}

        def json(self):
            return {"ok": True, "status": "pending"}

    class FakeClient:
        def __init__(self, *args, **kwargs): ...
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return FakeResponse()

    monkeypatch.setattr(callback.httpx, "Client", FakeClient)
    result = callback.provision_org("org1", None, "", "cedric")

    assert result and result.status_code == 202
    assert calls == [
        (
            "https://cedric/api/laura/orgs/pending",
            {
                "org_id": "org1",
                "team_id": None,
                "default_slack_channel": "",
                "avatar_id": "cedric",
            },
        )
    ]


def test_brain_connectors_not_connected_until_linked(client):
    """A fresh org with NO brain connection reads not_connected (never the
    global demo team's catalog); an install mid-flow reads pending."""
    user = _login(client)
    r = client.get("/dashboard/connections/brain/connectors")
    assert r.status_code == 200 and r.json()["status"] == "not_connected"
    assert r.json()["connectors"] == []
    store.set_connection(user["org_id"], "cedric", "cedric-brain", "pending", {})
    r = client.get("/dashboard/connections/brain/connectors")
    assert r.status_code == 200 and r.json()["status"] == "pending"


def test_brain_connectors_proxies_when_linked(client, monkeypatch):
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    monkeypatch.setattr(
        cedric, "provision_org", lambda *a, **k: callback.ProvisionResult(200, "minted")
    )
    monkeypatch.setattr(secret_registry, "upsert_org_secret", lambda *a: True)
    _login(client)
    client.post(
        "/dashboard/connections/brain",
        json={"avatar_id": "cedric", "team_id": "T1"},
    )
    payload = {
        "org_id": "x", "team_id": "T1", "manage_url": "https://cedric/integrations?team=T1",
        "connectors": [{"key": "gmail", "name": "Gmail", "connected": False,
                        "connect_url": "https://cedric/api/connect/google/start?team=T1&app=gmail"}],
    }
    monkeypatch.setattr(cedric, "fetch_org_connectors", lambda org, team="": payload)
    r = client.get("/dashboard/connections/brain/connectors")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["connectors"][0]["key"] == "gmail"
    assert "manage_url" in body


def test_brain_connectors_requires_login(client):
    r = client.get("/dashboard/connections/brain/connectors")
    assert r.status_code == 401
