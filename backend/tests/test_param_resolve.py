"""Semantic parameter gate: "you can't move if the parameters don't work".

Live card e44f90f7ee62497d (2026-07-22): project="jj" / assignee="hh" passed
shape validation, got approved, and died at the vendor with an opaque
"asana API returned 400". Three behaviours under test:

  1. RESOLVER — names resolve against the org's REAL workspace (native first,
     Pipedream proxy fallback); resolved names are rewritten to gids; unknown
     values produce field errors carrying the valid options; an unlistable
     workspace FAILS OPEN (we cannot validate what we cannot see).
  2. GATES — apply_param_edits and the dashboard approve door both refuse to
     move an action forward while a field doesn't resolve (422 + needs_details),
     and both persist gid rewrites so the executor acts on ids, never guesses.
  3. RECEIPTS — a vendor 4xx that still happens surfaces the vendor's OWN
     message, not just the status code.

Key-free: every vendor seam is monkeypatched.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipedream_executor, store
from app.actions import param_resolve
from app.integrations import asana_client


_DIR = {
    "ok": True,
    "projects": [{"gid": "1111", "name": "Monitoraggio"},
                 {"gid": "2222", "name": "Q3 Launch"}],
    "users": [{"gid": "9001", "name": "Duccio Profeti", "email": "d@x.com"},
              {"gid": "9002", "name": "Anant Rao", "email": "a@x.com"}],
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    param_resolve._cache.clear()
    yield
    param_resolve._cache.clear()


def _typed(project="Monitoraggio", assignee="Anant Rao"):
    return {"type": "asana.create_task",
            "args": {"name": "Pipedream connection", "project": project,
                     "assignee": assignee, "due_on": "2026-07-23"}}


# ── 1. resolver ──────────────────────────────────────────────────────────────

def test_names_resolve_to_gids(monkeypatch):
    monkeypatch.setattr(param_resolve, "asana_directory", lambda org: _DIR)
    rewrites, errors = param_resolve.validate_typed_params("org1", _typed())
    assert errors == []
    assert rewrites == {"project": "1111", "assignee": "9002"}


def test_unknown_values_error_with_options(monkeypatch):
    monkeypatch.setattr(param_resolve, "asana_directory", lambda org: _DIR)
    rewrites, errors = param_resolve.validate_typed_params(
        "org1", _typed(project="jj", assignee="hh")
    )
    assert rewrites == {}
    fields = {e["field"] for e in errors}
    assert fields == {"project", "assignee"}
    by_field = {e["field"]: e for e in errors}
    assert "Monitoraggio" in by_field["project"]["options"]
    assert "Anant Rao" in by_field["assignee"]["options"]


def test_gids_me_and_empty_pass_untouched(monkeypatch):
    monkeypatch.setattr(param_resolve, "asana_directory", lambda org: _DIR)
    typed = {"type": "asana.create_task",
             "args": {"name": "t", "project": "1111", "assignee": "me"}}
    assert param_resolve.validate_typed_params("org1", typed) == ({}, [])
    typed2 = {"type": "asana.create_task", "args": {"name": "t"}}
    assert param_resolve.validate_typed_params("org1", typed2) == ({}, [])


def test_unlistable_workspace_fails_open(monkeypatch):
    monkeypatch.setattr(
        param_resolve, "asana_directory",
        lambda org: {"ok": False, "projects": [], "users": []},
    )
    rewrites, errors = param_resolve.validate_typed_params(
        "org1", _typed(project="jj", assignee="hh")
    )
    assert rewrites == {} and errors == []


def test_non_asana_types_are_ignored():
    typed = {"type": "email.send", "args": {"to": ["a@b.c"]}}
    assert param_resolve.validate_typed_params("org1", typed) == ({}, [])


def test_directory_prefers_native_then_pipedream(monkeypatch):
    monkeypatch.setattr(
        asana_client, "list_projects",
        lambda org, **kw: {"ok": True,
                           "projects": [{"gid": "1111", "name": "Native P"}]},
    )
    monkeypatch.setattr(
        asana_client, "list_users",
        lambda org, **kw: {"ok": False, "error": "no token"},
    )
    monkeypatch.setattr(param_resolve, "_pd_asana_account", lambda org: "apn_1")
    monkeypatch.setattr(
        pipedream_executor, "_asana_workspace", lambda org, acct: "ws1"
    )
    monkeypatch.setattr(
        param_resolve, "_pd_get",
        lambda org, acct, url: (
            [{"gid": "9001", "name": "PD User", "email": "p@x.com"}]
            if "/users" in url else []
        ),
    )
    d = param_resolve.asana_directory("org-mixed")
    assert d["ok"]
    assert d["projects"][0]["name"] == "Native P"      # native won
    assert d["users"][0]["name"] == "PD User"          # proxy filled the gap


# ── 2. the gates ─────────────────────────────────────────────────────────────

@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    from app import ledger as ledger_mod
    import app.main as main_module
    from app.config import settings

    importlib.reload(store)
    importlib.reload(ledger_mod)
    monkeypatch.setattr(settings, "native_executor", True)
    from fastapi.testclient import TestClient

    return TestClient(main_module.app)


def _login(client):
    from app import auth

    user = store.upsert_user("owner@x.com")
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def _seed(org, aid="pr1", project="jj", assignee="hh"):
    action = {"item": "create the task", "action_id": aid, "owner": "Kai",
              "typed": _typed(project=project, assignee=assignee)}
    store.save_artifact(
        "bot_" + aid,
        {"summary": "s", "actions": [action], "checklist": [action],
         "org_id": org, "avatar_id": "laura",
         "meeting_url": "https://meet.google.com/pr-test"},
        org_id=org,
    )


def test_param_edit_with_unknown_project_422s_and_blocks(app_client, monkeypatch):
    from app import ledger

    monkeypatch.setattr(param_resolve, "asana_directory", lambda org: _DIR)
    user = _login(app_client)
    _seed(user["org_id"])
    r = app_client.post(
        "/dashboard/actions/pr1/params",
        json={"args": {"project": "jj", "assignee": "hh"}},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "invalid_params"
    fields = {e["field"] for e in body["invalid_fields"]}
    assert fields == {"project", "assignee"}
    assert "Monitoraggio" in body["invalid_fields"][0]["options"] or \
           "Monitoraggio" in body["invalid_fields"][1]["options"]
    st = ledger.action_statuses(["pr1"], org_id=user["org_id"])["pr1"]
    assert st["status"] == "needs_details"
    assert "invalid" in st["detail"]


def test_param_edit_with_real_names_rewrites_to_gids(app_client, monkeypatch):
    from app import ledger

    monkeypatch.setattr(param_resolve, "asana_directory", lambda org: _DIR)
    user = _login(app_client)
    _seed(user["org_id"], aid="pr2")
    r = app_client.post(
        "/dashboard/actions/pr2/params",
        json={"args": {"project": "Monitoraggio", "assignee": "Anant Rao"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "proposed"
    assert body["params"]["project"] == "1111"
    assert body["params"]["assignee"] == "9002"


def test_approve_blocks_until_params_resolve(app_client, monkeypatch):
    from app import executor, ledger

    monkeypatch.setattr(param_resolve, "asana_directory", lambda org: _DIR)
    called: list = []
    monkeypatch.setattr(
        executor, "execute_approved",
        lambda org, aid, act: called.append(aid) or {"ok": True},
    )
    user = _login(app_client)
    _seed(user["org_id"], aid="pr3")
    r = app_client.post("/dashboard/actions/pr3/approve")
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "needs_details"
    assert {e["field"] for e in body["invalid_fields"]} == {"project", "assignee"}
    assert not called, "nothing may execute while a field doesn't resolve"
    st = ledger.action_statuses(["pr3"], org_id=user["org_id"])["pr3"]
    assert st["status"] == "needs_details"


# ── 3. honest vendor receipts ────────────────────────────────────────────────

def test_api_error_detail_surfaces_vendor_message():
    resp = {"status": 400, "json": {"errors": [
        {"message": "assignee: Not a recognized ID: hh"}]}}
    out = pipedream_executor._api_error_detail("asana", resp)
    assert out == "asana API returned 400 — assignee: Not a recognized ID: hh"
    google = {"status": 403, "json": {"error": {"message": "insufficient scope"}}}
    assert "insufficient scope" in pipedream_executor._api_error_detail(
        "gmail", google)
    bare = {"status": 500, "json": None}
    assert pipedream_executor._api_error_detail("asana", bare) == (
        "asana API returned 500")
