"""Execution provenance loop: Cedric reports where each action stands
(POST /org/actions/{id}/status) and the dashboard shows it per action.
Also: every callback event carries org_id, and the signing secret is chosen
from the per-org registry (LAURA_WEBHOOK_SECRETS_BY_ORG) with global fallback.
Key-free like the rest of the suite."""
from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import ledger, store
from app.cedric import callback, secret_registry
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


def _seed_action(client, action_id: str = "abc123def456") -> None:
    """Save one artifact whose action carries a stable action_id, so the
    ledger row and the dashboard meeting both exist."""
    artifact = {
        "summary": "Weekly sync",
        "actions": [{"action_id": action_id, "item": "Send the deck", "owner": "duccio"}],
        "checklist": [],
        "decisions": [],
        "missing_steps": [],
        "avatar_id": "cedric",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "duration_seconds": 600,
    }
    store.save_artifact("bot-status-1", artifact)
    ledger.record_meeting(
        meeting_url="https://meet.google.com/abc-defg-hij", avatar_id="cedric",
        bot_id="bot-status-1", artifact=artifact,
    )


# ── the status endpoint ──

def test_status_upsert_and_dashboard_exposure(client):
    _seed_action(client, "abc123def456")
    r = client.post("/org/actions/abc123def456/status", json={"status": "proposed"})
    assert r.status_code == 200 and r.json()["recorded"] is True

    # latest wins
    r = client.post(
        "/org/actions/abc123def456/status",
        json={"status": "failed", "detail": "gmail auth expired"},
    )
    assert r.status_code == 200

    acts = client.get("/dashboard/summary").json()["meetings"][0]["actions"]
    ex = next(a for a in acts if a["action_id"] == "abc123def456")["execution"]
    assert ex["status"] == "failed"
    assert ex["detail"] == "gmail auth expired"


def test_status_done_closes_ledger_item(client):
    _seed_action(client, "feedbeef0001")
    r = client.post("/org/actions/feedbeef0001/status", json={"status": "done"})
    assert r.status_code == 200
    # the ledger item is closed exactly as if /resolve had been called
    open_items = [
        i for i in ledger.items(ledger.meeting_key("https://meet.google.com/abc-defg-hij"))
        if i["action_id"] == "feedbeef0001" and i["status"] == "open"
    ]
    assert open_items == []


def test_status_rejects_unknown_state_and_bad_json(client):
    r = client.post("/org/actions/whatever/status", json={"status": "exploded"})
    assert r.status_code == 400
    r = client.post(
        "/org/actions/whatever/status",
        content=b"not json", headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_status_respects_bearer_gate(client, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "sekret")
    r = client.post("/org/actions/x/status", json={"status": "done"})
    assert r.status_code == 401
    r = client.post(
        "/org/actions/x/status", json={"status": "proposed"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code in (200, 400)  # authed; body validity is separate


def test_status_detail_capped(client):
    ledger.set_action_status("cap0001", "proposed", "x" * 1000)
    assert len(ledger.action_statuses(["cap0001"])["cap0001"]["detail"]) == 300


# ── org_id on the wire + per-org signing ──

def test_callback_payloads_carry_org_id(monkeypatch):
    sent: list[dict] = []

    class _Resp:
        status_code = 200

    def fake_post(url, payload):
        sent.append(payload)
        return _Resp()

    monkeypatch.setattr(callback, "_post", fake_post)
    integ = {"callback_url": "https://cedric/events", "org_id": "org-42", "external_ref": {}}
    callback.send_status(integ, "bot1", "live")
    callback.send_action_requested(integ, "bot1", {"action_id": "a1", "action": "do it"})
    callback.send_ended(integ, "bot1", {"summary": "s", "actions": []})
    assert [p["org_id"] for p in sent] == ["org-42", "org-42", "org-42"]
    assert sent[1]["action_id"] == "a1"


def test_signing_secret_per_org_with_fallback(monkeypatch):
    # Pure env-registry behaviour: disable the SSM source so the lookup doesn't
    # reach a real parameter (the registry now consults SSM on the first call
    # after boot).
    monkeypatch.setattr(settings, "laura_webhook_registry_ssm_parameter", "")
    monkeypatch.setattr(settings, "laura_webhook_secret", "global-secret")
    monkeypatch.setattr(
        settings, "laura_webhook_secrets_by_org", json.dumps({"org-42": "org-secret"})
    )
    assert callback._secret_for("org-42") == "org-secret"
    assert callback._secret_for("org-other") == "global-secret"
    assert callback._secret_for("") == "global-secret"
    # a broken registry never blocks sending — falls back to the global secret
    monkeypatch.setattr(settings, "laura_webhook_secrets_by_org", "{not json")
    assert callback._secret_for("org-42") == "global-secret"


def test_ssm_registry_merge_preserves_existing_orgs_and_hot_reloads(monkeypatch):
    writes: list[dict] = []

    class FakeSsm:
        def get_parameter(self, **kwargs):
            assert kwargs["WithDecryption"] is True
            return {"Parameter": {"Value": json.dumps({"org-old": "old-secret"})}}

        def put_parameter(self, **kwargs):
            writes.append(kwargs)

    monkeypatch.setattr(secret_registry, "_client", lambda: FakeSsm())
    monkeypatch.setattr(settings, "laura_webhook_registry_ssm_parameter", "/test/registry")
    monkeypatch.setattr(settings, "laura_webhook_secrets_by_org", "{}")
    monkeypatch.setattr(secret_registry, "_env_snapshot", None)
    monkeypatch.setattr(secret_registry, "_cache", {})

    assert secret_registry.upsert_org_secret("org-new", "new-secret") is True
    dedicated = next(w for w in writes if w["Name"].endswith("/orgs/org-new"))
    aggregate = next(w for w in writes if w["Name"] == "/test/registry")
    assert dedicated["Value"] == "new-secret"
    assert dedicated["Type"] == "SecureString"
    assert dedicated["Overwrite"] is True
    saved = json.loads(aggregate["Value"])
    assert saved == {"org-old": "old-secret", "org-new": "new-secret"}
    assert aggregate["Type"] == "SecureString"
    assert aggregate["Overwrite"] is True
    assert callback._secret_for("org-new") == "new-secret"


def test_dedicated_ssm_secret_wins_over_stale_aggregate(monkeypatch):
    class FakeSsm:
        def get_parameter(self, **kwargs):
            return {"Parameter": {"Value": json.dumps({"org-new": "stale"})}}

        def get_parameters_by_path(self, **kwargs):
            assert kwargs["WithDecryption"] is True
            return {
                "Parameters": [
                    {
                        "Name": "/test/registry/orgs/org-new",
                        "Value": "durable-secret",
                    }
                ]
            }

    monkeypatch.setattr(secret_registry, "_client", lambda: FakeSsm())
    monkeypatch.setattr(settings, "laura_webhook_registry_ssm_parameter", "/test/registry")
    monkeypatch.setattr(settings, "laura_webhook_secrets_by_org", "{}")
    monkeypatch.setattr(secret_registry, "_env_snapshot", None)
    monkeypatch.setattr(secret_registry, "_cache", {})
    monkeypatch.setattr(secret_registry, "_last_ssm_refresh", float("-inf"))

    assert callback._secret_for("org-new") == "durable-secret"


def test_signature_differs_by_org_secret(monkeypatch):
    monkeypatch.setattr(settings, "laura_webhook_registry_ssm_parameter", "")
    monkeypatch.setattr(settings, "laura_webhook_secret", "global-secret")
    monkeypatch.setattr(settings, "laura_webhook_token", "tok")
    monkeypatch.setattr(
        settings, "laura_webhook_secrets_by_org", json.dumps({"org-42": "org-secret"})
    )
    body = b'{"event":"session.status"}'
    h_global = callback._signature_headers(body)
    h_org = callback._signature_headers(body, "org-42")
    assert h_global["Authorization"] == h_org["Authorization"] == "Bearer tok"
    assert h_global["X-Laura-Signature"] != h_org["X-Laura-Signature"]
