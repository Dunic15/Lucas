"""System Check page — /dashboard/syscheck (readiness board) and
/dashboard/syscheck/test (live read-only probes).

Key-free like the rest of the suite (sqlite in tmp_path, no vendors touched).
The invariants under test:
  * the board lists rows with the documented shape;
  * with Pipedream disabled the brokered rows read 'off' — never 'error', never
    a 500 (the key-free demo must stay byte-safe);
  * the four-worlds auth gate returns 401 {auth_enabled} when login is on and
    the caller is anonymous;
  * NO credential/token string ever appears in either payload;
  * a simulated 401 from the proxy maps to status 'error';
  * the graphiti probe only ever recalls the throwaway __smoke__ group — never
    a real org's graph.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, ledger, store
from app.api import dashboard as dash
from app.config import settings

ROW_KEYS = {
    "key", "label", "category", "status", "account", "detail",
    "last_checked", "supports", "can_test",
}
_STATUSES = {"ok", "warn", "off", "error"}

SECRET_RT = "SECRET_RT_should_never_leak_1a2b3c"


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


# ── (1) the board shape ─────────────────────────────────────────────────────

def test_syscheck_returns_rows_with_documented_keys(client):
    resp = client.get("/dashboard/syscheck")
    assert resp.status_code == 200
    body = resp.json()
    assert "generated_at" in body and isinstance(body["rows"], list)
    assert body["rows"], "expected at least one provider row"
    keys = {r["key"] for r in body["rows"]}
    # The documented provider set is present.
    assert {"google_calendar", "gmail", "google_drive", "asana", "slack",
            "meeting_bot", "voice", "company_brain", "graphiti"} <= keys
    for r in body["rows"]:
        assert ROW_KEYS <= set(r), f"row {r.get('key')} missing keys"
        assert r["status"] in _STATUSES
        assert isinstance(r["supports"], list)
        assert isinstance(r["can_test"], bool)
    # no-store on a dynamic per-tenant read
    assert "no-store" in resp.headers.get("cache-control", "")


# ── (2) Pipedream disabled ⇒ brokered rows 'off', never error / 500 ─────────

def test_pipedream_disabled_rows_are_off_not_error(client):
    # Default key-free settings: Pipedream is not enabled.
    assert not dash.pipedream_client.enabled()
    resp = client.get("/dashboard/syscheck")
    assert resp.status_code == 200
    rows = {r["key"]: r for r in resp.json()["rows"]}
    # Brokered rows with no other signal read 'off' — never 'error'/500. (Drive
    # can legitimately be 'ok' via a shipped avatar's drive_folder_id, a NATIVE
    # signal independent of Pipedream, so it is checked only for not-error.)
    for key in ("google_calendar", "gmail", "asana"):
        assert rows[key]["status"] == "off", (key, rows[key])
    # Nothing on the board is a hard 'error' in the untouched key-free world.
    assert all(r["status"] != "error" for r in rows.values())


# ── (3) auth gate ───────────────────────────────────────────────────────────

def test_auth_gate_returns_401_auth_enabled_when_gated(client, monkeypatch):
    # Login enabled = a configured Google OAuth client.
    monkeypatch.setattr(settings, "google_calendar_client_id", "gci")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "gcs")
    resp = client.get("/dashboard/syscheck")
    assert resp.status_code == 401
    assert resp.json().get("auth_enabled") is True


# ── (4) no credential / token strings in either payload ─────────────────────

def test_no_credentials_in_payloads(client, monkeypatch):
    # A secret is needed to encrypt the token at rest (fails closed otherwise).
    monkeypatch.setattr(settings, "session_secret", "test-enc-secret")
    # Seed a real (encrypted-at-rest) Google grant for the demo org; the board
    # may surface the connected EMAIL but must never echo the refresh token.
    store.set_org_oauth(
        settings.demo_org_id, SECRET_RT, email="acct@x.com", scopes="cal")
    get_resp = client.get("/dashboard/syscheck")
    assert get_resp.status_code == 200
    assert SECRET_RT not in get_resp.text
    # The connected account label IS allowed to appear (it is not a secret).
    rows = {r["key"]: r for r in get_resp.json()["rows"]}
    assert rows["google_calendar"]["status"] == "ok"
    assert rows["google_calendar"]["account"] == "acct@x.com"

    # POST probe payload likewise carries no token (unconfigured provider → off).
    _login(client)
    post_resp = client.post("/dashboard/syscheck/test", json={"provider": "slack"})
    assert post_resp.status_code == 200
    assert SECRET_RT not in post_resp.text


# ── (5) a simulated 401 proxy response ⇒ status 'error' ─────────────────────

def test_probe_maps_401_to_error(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(dash.pipedream_client, "enabled", lambda: True)
    monkeypatch.setattr(dash, "_pd_account_id", lambda org, slug: "acct_1")
    monkeypatch.setattr(
        dash.pipedream_client, "proxy_request",
        lambda org, acct, method, url, **kw: {"status": 401, "ok": False},
    )
    resp = client.post("/dashboard/syscheck/test", json={"provider": "asana"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "asana"
    assert body["status"] == "error"
    assert "reconnect" in body["detail"]
    assert "account" in body and isinstance(body["latency_ms"], int)


def test_probe_requires_login_and_same_origin(client, monkeypatch):
    # Anonymous + login enabled → gated.
    monkeypatch.setattr(settings, "google_calendar_client_id", "gci")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "gcs")
    resp = client.post("/dashboard/syscheck/test", json={"provider": "asana"})
    assert resp.status_code == 401


# ── (6) graphiti probe only ever touches the __smoke__ group ────────────────

def test_graphiti_probe_never_recalls_a_real_org(client, monkeypatch):
    _login(client)
    recall_orgs: list[str] = []

    async def _ensure_ready():
        return True

    async def _ingest(org, *a, **k):
        recall_orgs.append(("ingest", org))
        return True

    async def _recall(org, query, **k):
        recall_orgs.append(("recall", org))
        return "- ENG-999 is assigned to Dana Lin"

    monkeypatch.setattr(dash.graphiti_client, "enabled", lambda: True)
    monkeypatch.setattr(dash.graphiti_client, "ensure_ready", _ensure_ready)
    monkeypatch.setattr(dash.graphiti_client, "ingest", _ingest)
    monkeypatch.setattr(dash.graphiti_client, "recall", _recall)

    resp = client.post("/dashboard/syscheck/test", json={"provider": "graphiti"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    # Every graph call — ingest AND recall — used the throwaway smoke group only.
    assert recall_orgs, "expected the probe to exercise the graph"
    assert all(org == "__smoke__" for _kind, org in recall_orgs), recall_orgs


def test_graphiti_probe_off_when_disabled(client, monkeypatch):
    _login(client)
    assert not dash.graphiti_client.enabled()  # key-free default
    resp = client.post("/dashboard/syscheck/test", json={"provider": "graphiti"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "off"
