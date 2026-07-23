"""Approving an action is executing it — scope the door like the meeting.

Security audit 2026-07-23, gap #3. ``_find_org_action`` checked the ORG only,
so every action door (approve, reject, params, read) was open to any logged-in
member of the org. A colleague who never attended a meeting could approve —
and therefore EXECUTE, against the org's connected Google/Asana accounts — an
action captured in a conversation they were never part of.

The meeting surfaces already scope per person (``_user_attended``); the action
doors now apply the same rule. A machine/service caller (user=None) keeps the
org-wide scope it has always had.

NOTE the standing limitation this does NOT fix (audit gap #5): _user_attended
still fails OPEN when an artifact has no principal and no transcript speakers,
and matches on ASR display names. Tightening that is a separate change; this
one closes the "any member executes anything" hole.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth
from app.config import settings

TYPED = {
    "type": "email.send",
    "args": {"to": ["board@example.com"], "subject": "Q3", "body": "text"},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    from app import store as store_mod
    from app import ledger as ledger_mod
    importlib.reload(store_mod)
    importlib.reload(ledger_mod)
    import app.main as main_module
    from fastapi.testclient import TestClient

    # One shared company org for everyone in the domain.
    monkeypatch.setattr(settings, "shared_domain_orgs", True)
    with store_mod._LOCK, store_mod._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO org_domains (domain, org_id, verified_at) "
            "VALUES (?, ?, datetime('now'))", ("acme.example", "org-acme"))

    attendee = store_mod.upsert_user("attendee@acme.example", name="Kai Rossi")
    outsider = store_mod.upsert_user("outsider@acme.example", name="Sam Doe")
    org = attendee["org_id"]
    assert outsider["org_id"] == org, "both must be in the same company org"

    store_mod.save_artifact(
        "bot-scope-1",
        {
            "summary": "Board prep.",
            "org_id": org,
            "avatar_id": "laura",
            "meeting_url": "https://meet.google.com/acme-board",
            # Kai spoke; Sam was never in the room.
            "transcript": "Kai Rossi: send the board the Q3 numbers",
            "actions": [{"action_id": "act-1", "item": "email the board",
                         "typed": TYPED}],
        },
        org_id=org,
    )
    return main_module, store_mod, TestClient(main_module.app), attendee, outsider


def _as(client, user):
    client.cookies.clear()
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))


# ── the hole ───────────────────────────────────────────────────────────────

def test_non_attendee_cannot_approve_and_nothing_executes(env, monkeypatch):
    main_module, store_mod, client, attendee, outsider = env
    from app.actions import executor

    ran: list = []
    monkeypatch.setattr(
        executor, "execute_approved",
        lambda org, aid, action: ran.append(aid) or {"ok": True},
    )
    _as(client, outsider)
    r = client.post("/dashboard/actions/act-1/approve")
    assert r.status_code == 404, r.text
    assert not ran, "a colleague who never attended must not execute the action"


def test_non_attendee_cannot_reject(env):
    main_module, store_mod, client, attendee, outsider = env
    _as(client, outsider)
    assert client.post("/dashboard/actions/act-1/reject").status_code == 404


def test_non_attendee_cannot_edit_the_parameters(env):
    """Editing params is pre-execution tampering — same door, same scope."""
    main_module, store_mod, client, attendee, outsider = env
    _as(client, outsider)
    r = client.post(
        "/dashboard/actions/act-1/params",
        json={"args": {"to": ["attacker@evil.example"]}},
    )
    assert r.status_code in (403, 404), r.text


# ── the people who SHOULD get through ──────────────────────────────────────

def test_attendee_still_approves(env, monkeypatch):
    main_module, store_mod, client, attendee, outsider = env
    from app.actions import executor

    ran: list = []
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(
        executor, "execute_approved",
        lambda org, aid, action: ran.append(aid) or {"ok": True},
    )
    _as(client, attendee)
    r = client.post("/dashboard/actions/act-1/approve")
    assert r.status_code == 200, r.text
    assert ran == ["act-1"], "the person who was in the meeting must still act"


def test_machine_caller_keeps_org_wide_scope(env):
    """Service callers (Cedric, org tokens) have no attendance to check."""
    main_module, store_mod, client, attendee, outsider = env
    from app.api import dashboard as dash

    found = dash._find_org_action(str(attendee["org_id"]), "act-1", None)
    assert found is not None and found[0]["action_id"] == "act-1"
