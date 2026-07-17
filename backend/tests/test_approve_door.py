"""handshake operation approve-action + routing stamps + action-events client.

Contract tests for the agreed action-lifecycle contract
(hsk_con_cnw4567mqj3p49dyn3dg): the canonical approval door's auth rules,
decision-based convergence (replay vs conflict), dependency gating, slot
rules, route-based execution, the finalize routing stamps, and the A->B
events envelope. Key-free: fresh temp SQLite, executor/Google mocked.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import executor, ledger, store
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


def _seed(org: str, action_id: str, **extra) -> None:
    action = {"item": "Email the recap to dana@acme.com", "owner": "Ben",
              "action_id": action_id, **extra}
    store.save_artifact(
        f"bot_{action_id}",
        {"summary": "Kickoff.", "actions": [action], "checklist": [action],
         "org_id": org, "avatar_id": "laura",
         "meeting_url": "https://meet.google.com/apr-door",
         "transcript": "PII must never leak"},
        org_id=org,
    )


_TYPED = {"type": "email.send",
          "args": {"to": ["dana@acme.com"], "subject": "Recap", "body": "Notes"}}


def _approve(client, aid, **body):
    return client.post(f"/org/actions/{aid}/approve",
                       json={"decision": "approve", **body})


# ── auth rules [B1] ──

def test_global_bearer_with_foreign_org_header_is_401(client, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "global-tok")
    r = client.post("/org/actions/a1/approve",
                    headers={"Authorization": "Bearer global-tok",
                             "X-Laura-Org-Id": "org-elsewhere"},
                    json={"decision": "approve"})
    assert r.status_code == 401 and r.json()["error"] == "org_token_required"


def test_org_header_mismatch_is_404(client):
    _seed(settings.demo_org_id, "a1")
    r = client.post("/org/actions/a1/approve",
                    headers={"X-Laura-Org-Id": "some-other-org"},
                    json={"decision": "approve"})
    assert r.status_code == 404  # indistinguishable from unknown action


def test_unknown_action_404_and_bad_decision_400(client):
    assert _approve(client, "nope").status_code == 404
    _seed(settings.demo_org_id, "a1")
    r = client.post("/org/actions/a1/approve", json={"decision": "maybe"})
    assert r.status_code == 400


# ── decision-based convergence [M1] ──

def test_approve_then_replay_then_conflict(client, monkeypatch):
    _seed(settings.demo_org_id, "a1")  # untyped → route=cedric → approved
    r1 = _approve(client, "a1", idempotency_key="k1", decided_via="dashboard")
    b1 = r1.json()
    assert r1.status_code == 200 and b1["new_status"] == "approved"
    assert b1["execution_job_id"] is None and b1["idempotent_replay"] is False

    # (b) different key, SAME decision → replay, never a 409.
    r2 = _approve(client, "a1", idempotency_key="k2", decided_via="slack")
    b2 = r2.json()
    assert r2.status_code == 200 and b2["idempotent_replay"] is True
    assert b2["new_status"] == b1["new_status"]

    # (c) conflicting decision → 409 decision_conflict with attribution.
    r3 = client.post("/org/actions/a1/approve", json={"decision": "reject"})
    b3 = r3.json()
    assert r3.status_code == 409 and b3["error"] == "decision_conflict"
    assert b3["decided_via"] == "dashboard" and b3["current_status"] == "approved"


def test_native_route_executes_exactly_once(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    _seed(settings.demo_org_id, "a1", typed=_TYPED, execution_route="native")
    calls: list = []
    monkeypatch.setattr(
        executor.google_client, "send_gmail",
        lambda org, msg: calls.append(msg) or {"ok": True, "message_id": "m1"},
    )
    r1 = _approve(client, "a1", laura_user_id="", decided_via="slack")
    b1 = r1.json()
    assert b1["new_status"] == "done" and b1["execution_job_id"]
    assert len(calls) == 1
    r2 = _approve(client, "a1")  # replay
    assert r2.json()["idempotent_replay"] is True
    assert len(calls) == 1  # never a second vendor call


def test_reject_and_respond_transitions(client):
    org = settings.demo_org_id
    _seed(org, "a1")
    r = client.post("/org/actions/a1/approve",
                    json={"decision": "reject", "decided_via": "slack"})
    assert r.json()["new_status"] == "rejected"
    st = ledger.action_statuses(["a1"], org_id=org).get("a1")
    assert st and st["status"] == "rejected"

    _seed(org, "a2")
    r2 = client.post("/org/actions/a2/approve",
                     json={"decision": "respond", "response_text": "45 minutes"})
    assert r2.json()["new_status"] == "done"


# ── dependency gate [M8] ──

def test_approve_with_unmet_dependency_blocks_execution(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    org = settings.demo_org_id
    _seed(org, "dep1")  # the dependency, not done
    _seed(org, "a1", typed=_TYPED, execution_route="native",
          dependencies=["dep1"])
    calls: list = []
    monkeypatch.setattr(
        executor.google_client, "send_gmail",
        lambda o, m: calls.append(m) or {"ok": True, "message_id": "m1"},
    )
    r = _approve(client, "a1")
    b = r.json()
    assert b["new_status"] == "approved" and b["execution_job_id"] is None
    assert b["blocked_on"] == ["dep1"]
    assert not calls  # nothing executed while blocked


# ── slot rules [M9] ──

_PROPOSAL = {"candidate_slots": [
    {"slot_id": "s1", "start": "2020-01-01T10:00:00", "end": "2020-01-01T10:45:00", "rank": 1},
    {"slot_id": "s2", "start": "2099-01-01T10:00:00", "end": "2099-01-01T10:45:00", "rank": 2},
]}


def test_slot_selection_rules(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    org = settings.demo_org_id
    _seed(org, "a1", proposal=_PROPOSAL,
          typed={"type": "calendar.create_event",
                 "args": {"title": "Follow-up", "start": "", "end": ""}},
          execution_route="native")
    # Unknown slot → 422.
    assert _approve(client, "a1", selected_slot_id="nope").status_code == 422
    # Past slot → 409 slot_stale.
    r = _approve(client, "a1", selected_slot_id="s1")
    assert r.status_code == 409 and r.json()["error"] == "slot_stale"
    # Valid future slot → executes with the materialized times.
    seen: list = []
    monkeypatch.setattr(
        executor.google_client, "create_calendar_event",
        lambda o, ev: seen.append(ev) or {"ok": True, "event_id": "e1",
                                          "event_url": "https://cal/e1"},
    )
    r2 = _approve(client, "a1", selected_slot_id="s2")
    assert r2.json()["new_status"] == "done"
    assert seen[0]["start"].startswith("2099-01-01T10:00")


# ── route=cedric: approval dispatches to the orchestrator [B2] ──

def test_org_door_approve_dispatches_cedric_route(client, monkeypatch):
    from app.cedric import callback

    org = settings.demo_org_id
    _seed(org, "a1", execution_route="cedric")
    sent: list = []
    monkeypatch.setattr(
        callback, "dispatch_action",
        lambda o, a, approved_by="": sent.append((o, a.get("action_id"))) or {"ok": True},
    )
    r = _approve(client, "a1")
    b = r.json()
    assert b["new_status"] == "approved" and b["execution_job_id"] is None
    assert sent == [(org, "a1")]  # handed to Cedric, exactly once
    r2 = _approve(client, "a1")  # replay never re-dispatches
    assert r2.json()["idempotent_replay"] is True
    assert len(sent) == 1


def test_dispatch_action_payload_shape(monkeypatch):
    from app.cedric import callback

    monkeypatch.setattr(settings, "cedric_orgs_url",
                        "https://ced.example/api/laura/orgs")
    captured: list = []

    class _R:
        status_code = 202

    def fake_post(url, payload, *, idempotency_key=""):
        captured.append((url, payload, idempotency_key))
        return _R()

    monkeypatch.setattr(callback, "_post", fake_post)
    monkeypatch.setattr(callback, "_team_id_for", lambda org: "T123")
    out = callback.dispatch_action(
        "org-a",
        {"action_id": "a9", "item": "Read three Slack channels", "owner": "JT",
         "correlation_id": "a9"},
        approved_by="user-1",
    )
    assert out["ok"] is True
    url, payload, idem = captured[0]
    assert url == "https://ced.example/api/laura/actions"
    assert idem == "a9" and payload["org_id"] == "org-a"
    act = payload["action"]
    assert act["approval_mode"] == "pre_approved"
    assert act["execution_route"] == "cedric"
    assert act["tenant"] == {"org_id": "org-a", "team_id": "T123"}
    assert act["type"] == "task.freeform"  # untyped → distilled item rides args
    assert act["args"]["item"] == "Read three Slack channels"
    assert act["approved_by"] == "user-1"


def test_dispatch_action_endpoint_missing_is_soft(monkeypatch):
    from app.cedric import callback

    monkeypatch.setattr(settings, "cedric_orgs_url",
                        "https://ced.example/api/laura/orgs")

    class _R:
        status_code = 404

    monkeypatch.setattr(callback, "_post", lambda *a, **k: _R())
    out = callback.dispatch_action("org-a", {"action_id": "a1"})
    assert out == {"ok": False, "reason": "dispatch_endpoint_missing"}
    monkeypatch.setattr(settings, "cedric_orgs_url", "")
    assert callback.dispatch_action("org-a", {"action_id": "a1"})["reason"] == "not_configured"


# ── finalize routing stamps (contract Action fields) ──

def test_stamp_action_routing(monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    actions = [
        {"item": "typed one", "action_id": "x1", "typed": _TYPED, "owner": "Ben"},
        {"item": "untyped one", "action_id": "x2", "owner": ""},
        {"item": "pre-routed", "action_id": "x3", "execution_route": "cedric",
         "typed": _TYPED, "owner": "Dana"},
    ]
    out = main_module._stamp_action_routing(actions)
    assert out[0]["execution_route"] == "native"
    assert out[0]["correlation_id"] == "x1"
    assert out[0]["execution_policy"] == "approval_required"
    assert "unresolved_roles" not in out[0]
    assert out[1]["execution_route"] == "cedric"  # no typed spec
    assert out[1]["unresolved_roles"] == ["owner"]
    assert out[1]["unassigned_reason"] == "no_owner_rule_match"
    assert out[2]["execution_route"] == "cedric"  # persisted route is immutable


# ── action-events client (A->B envelope) ──

def test_send_action_event_envelope(monkeypatch, tmp_path):
    from app.cedric import callback

    monkeypatch.setattr(settings, "cedric_orgs_url",
                        "https://ced.example/api/laura/orgs")
    assert callback.events_url() == "https://ced.example/api/laura/events"
    sent: list = []

    class _R:
        status_code = 200

    def fake_post(url, payload, **kw):
        sent.append((url, payload))
        return _R()

    monkeypatch.setattr(callback, "_post", fake_post)
    ok = callback.send_action_event("org-a", "action.status", {
        "action_id": "a1", "status": "done", "detail": "native · email · m1",
        "receipt_url": "",
    })
    assert ok
    url, payload = sent[0]
    assert url.endswith("/api/laura/events")
    assert payload["event"] == "action.status" and payload["org_id"] == "org-a"
    assert payload["correlation_id"] == "a1"  # defaults to action_id
    assert payload["event_id"]  # A-authored → fresh id minted


def test_send_action_event_noop_without_config(monkeypatch):
    from app.cedric import callback

    monkeypatch.setattr(settings, "cedric_orgs_url", "")
    assert callback.send_action_event("org-a", "action.status", {}) is False
