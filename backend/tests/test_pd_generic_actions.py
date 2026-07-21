"""Generic Pipedream execution plane — pd.<app>.run (toggle an app → the
avatar can run its PRE-BUILT actions, schema-gated, behind the approve door).

Key-free: pipedream_client (list_accounts/get_component/run_action) and the
ledger are monkeypatched; no real Pipedream or network calls. Asserts:
  * type parsing (generic_app) + handles() + per-family routing;
  * capability policy: generic apps are explicit OPT-IN per avatar (blocked
    unless toggled ON), native families keep default-ON semantics;
  * execute_approved(generic): schema gate — unknown props dropped, required
    props enforced, the auth prop NEVER model-writable, truthful failed
    receipts, run_action called with the account's authProvisionId;
  * store accepts slug capability keys; junk rejected;
  * brain _sanitize_typed accepts only offered apps/keys and caps prop sizes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import executor, pipedream_client, pipedream_executor, store
from app.config import settings


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

    from app import ledger

    monkeypatch.setattr(ledger, "set_action_status", fake_status)
    return seen


_SCHEMA = {
    "key": "github-create-issue",
    "name": "Create Issue",
    "configurable_props": [
        {"name": "github", "type": "app", "app": "github"},
        {"name": "owner", "type": "string"},
        {"name": "repo", "type": "string"},
        {"name": "title", "type": "string"},
        {"name": "body", "type": "string", "optional": True},
    ],
}


# ── type parsing + routing ──────────────────────────────────────────────────

def test_generic_app_parses_only_wellformed_types():
    assert pipedream_executor.generic_app("pd.github.run") == "github"
    assert pipedream_executor.generic_app("pd.google_sheets.run") == "google_sheets"
    assert pipedream_executor.generic_app("asana.create_task") == ""
    assert pipedream_executor.generic_app("pd..run") == ""
    assert pipedream_executor.generic_app("pd.GitHub.run") == ""  # slug case
    assert pipedream_executor.generic_app(None) == ""


def test_handles_and_route_for_generic(monkeypatch):
    _enable_pd(monkeypatch)
    typed = {"type": "pd.github.run",
             "args": {"action_key": "github-create-issue", "props": {}}}
    assert pipedream_executor.handles({"type": "pd.github.run"})
    assert executor.route_for_typed(typed) == "pipedream"
    # from_typed passes generic specs through (the approve doors need a shape).
    bridged = executor.from_typed(typed)
    assert bridged == {"type": "pd.github.run",
                       "args": {"action_key": "github-create-issue", "props": {}}}


def test_flag_off_is_byte_identical(monkeypatch):
    monkeypatch.setattr(settings, "pipedream_executor", False)
    monkeypatch.setattr(settings, "native_executor", True)
    typed = {"type": "pd.github.run", "args": {"action_key": "k"}}
    assert not pipedream_executor.handles({"type": "pd.github.run"})
    # Unknown to the native runtime too → cedric, exactly like before.
    assert executor.route_for_typed(typed) == "cedric"


def test_capability_family_for_generic_is_the_app():
    assert executor.capability_family("pd.github.run") == "github"
    assert executor.capability_family("pd.notion.run") == "notion"
    assert executor.capability_family("calendar.create_event") == "google"


def test_capability_policy_generic_is_opt_in():
    # Generic: blocked unless explicitly ON.
    assert executor.capability_blocked({}, "pd.github.run") is True
    assert executor.capability_blocked({"github": False}, "pd.github.run") is True
    assert executor.capability_blocked({"github": True}, "pd.github.run") is False
    # Native families keep default-ON semantics (blocked only on explicit OFF).
    # Google is governed by its SPLIT per-app toggle (gmail/google_calendar/
    # google_drive), not the legacy combined 'google' key.
    assert executor.capability_blocked({}, "calendar.create_event") is False
    assert executor.capability_blocked({"google_calendar": False}, "calendar.create_event") is True
    assert executor.capability_blocked({"gmail": False}, "email.send") is True
    # A stale combined 'google=False' row must NOT veto an individually-on app.
    assert executor.capability_blocked({"google": False, "gmail": True}, "email.send") is False


# ── store: slug capability keys ─────────────────────────────────────────────

def test_store_accepts_slug_capabilities(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    import importlib

    importlib.reload(store)
    assert store.set_avatar_capability("laura", "github", True) is True
    assert store.get_avatar_capabilities("laura") == {"github": True}
    assert store.set_avatar_capability("laura", "GitHub!", True) is False
    assert store.set_avatar_capability("laura", "", True) is False


# ── execute_approved: schema gate + receipts ────────────────────────────────

def _accounts(monkeypatch, accounts):
    monkeypatch.setattr(pipedream_client, "list_accounts",
                        lambda org, app="": accounts)


def test_generic_execute_happy_path(monkeypatch):
    _enable_pd(monkeypatch)
    seen = _cap_ledger(monkeypatch)
    _accounts(monkeypatch, [{"id": "apn_1", "app": "github", "healthy": True}])
    monkeypatch.setattr(pipedream_client, "get_component", lambda key: _SCHEMA)
    calls = {}

    def fake_run(org, action_id, props):
        calls.update(org=org, action_id=action_id, props=props)
        return {"exports": {"$summary": "Created issue #12"}}

    monkeypatch.setattr(pipedream_client, "run_action", fake_run)
    result = pipedream_executor.execute_approved(
        "org1", "a1",
        {"type": "pd.github.run",
         "args": {"action_key": "github-create-issue",
                  "props": {"owner": "acme", "repo": "site", "title": "Fix login",
                           "hacker_field": "dropped",
                           "github": {"authProvisionId": "apn_EVIL"}}}},
    )
    assert result["ok"] is True
    assert seen["status"] == "done"
    assert calls["action_id"] == "github-create-issue"
    # Auth prop comes from the resolved account — never from the model.
    assert calls["props"]["github"] == {"authProvisionId": "apn_1"}
    assert "hacker_field" not in calls["props"]
    assert calls["props"]["title"] == "Fix login"
    assert "Created issue #12" in seen["detail"]


def test_generic_execute_missing_required_fails_before_run(monkeypatch):
    _enable_pd(monkeypatch)
    seen = _cap_ledger(monkeypatch)
    _accounts(monkeypatch, [{"id": "apn_1", "app": "github", "healthy": True}])
    monkeypatch.setattr(pipedream_client, "get_component", lambda key: _SCHEMA)
    ran = []
    monkeypatch.setattr(pipedream_client, "run_action",
                        lambda *a, **k: ran.append(1) or {})
    result = pipedream_executor.execute_approved(
        "org1", "a2",
        {"type": "pd.github.run",
         "args": {"action_key": "github-create-issue",
                  "props": {"title": "no repo"}}},
    )
    assert result["ok"] is False and not ran
    assert seen["status"] == "failed"
    assert "missing required" in seen["detail"]


def test_generic_execute_wrong_app_key_fails(monkeypatch):
    _enable_pd(monkeypatch)
    seen = _cap_ledger(monkeypatch)
    _accounts(monkeypatch, [{"id": "apn_1", "app": "github", "healthy": True}])
    result = pipedream_executor.execute_approved(
        "org1", "a3",
        {"type": "pd.github.run",
         "args": {"action_key": "notion-create-page", "props": {}}},
    )
    assert result["ok"] is False
    assert seen["status"] == "failed"
    assert "doesn't belong" in seen["detail"]


def test_generic_execute_no_schema_refuses(monkeypatch):
    _enable_pd(monkeypatch)
    seen = _cap_ledger(monkeypatch)
    _accounts(monkeypatch, [{"id": "apn_1", "app": "github", "healthy": True}])
    monkeypatch.setattr(pipedream_client, "get_component", lambda key: {})
    result = pipedream_executor.execute_approved(
        "org1", "a4",
        {"type": "pd.github.run",
         "args": {"action_key": "github-create-issue", "props": {}}},
    )
    assert result["ok"] is False
    assert "definition" in seen["detail"]


def test_generic_execute_not_connected_fails_truthfully(monkeypatch):
    _enable_pd(monkeypatch)
    seen = _cap_ledger(monkeypatch)
    _accounts(monkeypatch, [])
    result = pipedream_executor.execute_approved(
        "org1", "a5",
        {"type": "pd.github.run",
         "args": {"action_key": "github-create-issue", "props": {}}},
    )
    assert result["ok"] is False
    assert "isn't connected" in seen["detail"]


# ── brain sanitizer: only offered apps/keys survive ─────────────────────────

def test_sanitize_typed_pd_branch():
    from app.brain import engine

    catalog = {"github": [{"key": "github-create-issue", "name": "Create Issue"}]}
    action = {"item": "Open a ticket for the login bug"}
    clean = engine._sanitize_typed(
        {"type": "pd.github.run",
         "args": {"action_key": "github-create-issue",
                  "props": {"title": "Login bug", "labels": ["bug"], "n": 3},
                  "summary": "Create a GitHub issue"}},
        action, "", pd_apps=catalog,
    )
    assert clean["type"] == "pd.github.run"
    assert clean["args"]["action_key"] == "github-create-issue"
    assert clean["args"]["props"]["labels"] == ["bug"]
    # Un-offered app or key → None (the model can't invent surfaces).
    assert engine._sanitize_typed(
        {"type": "pd.notion.run", "args": {"action_key": "notion-create-page"}},
        action, "", pd_apps=catalog) is None
    assert engine._sanitize_typed(
        {"type": "pd.github.run", "args": {"action_key": "github-delete-repo"}},
        action, "", pd_apps=catalog) is None
    # No offer at all → None.
    assert engine._sanitize_typed(
        {"type": "pd.github.run", "args": {"action_key": "github-create-issue"}},
        action, "", pd_apps=None) is None
