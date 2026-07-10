"""Connect the brain — the org→Slack-workspace link behind the Configure tab.
POST saves the wiring and provisions on Cedric when configured (connected) or
records it locally when not (pending); DELETE marks disconnected; the summary
exposes org_connections for the logged-in org only. Key-free."""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, cedric, ledger, store
from app.cedric import callback
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
        return True

    # dashboard.py calls the package re-export, so patch that binding
    monkeypatch.setattr(cedric, "provision_org", fake_provision)
    user = _login(client)
    r = client.post(
        "/dashboard/connections/brain",
        json={"avatar_id": "cedric", "team_id": "T9", "channel": "#ops"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "connected"
    assert calls == [(user["org_id"], "T9", "#ops", "cedric")]


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
