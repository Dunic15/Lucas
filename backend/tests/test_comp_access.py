"""Comped access (BILLING_COMP_EMAILS): full access, billing waived.

Key-free: the control plane stays off (grant_comp cleanly no-ops), the
comp plan's allowance math is unit-tested against the row shape, and the
login hook is asserted through the mocked Google callback — the same
machinery test_auth.py uses.
"""
from __future__ import annotations

import base64
import importlib
import json as _json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, entitlements, store
from app.config import settings


# ── the comp list ──

def test_comp_list_defaults_to_jt(monkeypatch):
    assert entitlements.is_comp_email("jt@sff.vc") is True
    assert entitlements.is_comp_email("JT@SFF.VC") is True  # case-insensitive
    assert entitlements.is_comp_email("someone-else@sff.vc") is False
    assert entitlements.is_comp_email("") is False


def test_comp_list_env_extends(monkeypatch):
    monkeypatch.setattr(settings, "billing_comp_emails", "a@x.com, b@y.com")
    assert entitlements.is_comp_email("b@y.com") is True
    assert entitlements.is_comp_email("jt@sff.vc") is False  # env overrides


# ── the allowance math ──

def test_comp_plan_allowance_is_effectively_unlimited():
    # Row shape: (included_seconds, plan, subscription_status, period_end, period_start)
    row = (900, "comp", "none", None, None)
    assert entitlements._effective_allowance(row) >= entitlements._COMP_ALLOWANCE
    # Free/solo semantics untouched.
    assert entitlements._effective_allowance((900, "free", None, None, None)) == 900
    assert entitlements._effective_allowance(None) == int(settings.free_trial_seconds)


def test_grant_comp_noops_when_control_plane_off():
    assert entitlements.enabled() is False  # key-free suite
    assert entitlements.grant_comp("org-x") is False  # nothing to waive


# ── the login hook ──

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid-test")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csec")
    monkeypatch.setattr(settings, "session_secret", "sek")
    return TestClient(main_module.app)


def _callback_login(client, monkeypatch, email: str):
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "cid-test",
        "exp": time.time() + 600,
        "email": email,
        "email_verified": True,
        "name": "JT",
    }
    body = base64.urlsafe_b64encode(_json.dumps(claims).encode()).decode().rstrip("=")
    fake_id_token = f"h.{body}.s"

    class FakeResp:
        status_code = 200

        def json(self):
            return {"id_token": fake_id_token}

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeClient)
    # Browser-bound signed state, same construction as test_auth._valid_state.
    payload = auth._b64(
        _json.dumps({"n": "nonce-comp", "exp": time.time() + 600}).encode()
    )
    client.cookies.set(auth.STATE_COOKIE, "nonce-comp")
    state = f"{payload}.{auth._sign(payload, 'state')}"
    return client.get(
        "/auth/google/callback",
        params={"code": "fake-code", "state": state},
        follow_redirects=False,
    )


def test_login_grants_comp_for_listed_email(client, monkeypatch):
    granted: list = []
    monkeypatch.setattr(
        entitlements, "grant_comp", lambda org: granted.append(org) or True
    )
    resp = _callback_login(client, monkeypatch, "jt@sff.vc")
    assert resp.status_code == 302
    uid = auth.read_cookie(resp.cookies[auth.COOKIE_NAME])
    user = store.get_user(uid)
    assert granted == [user["org_id"]]  # comp granted to jt's org, exactly once


def test_login_skips_comp_for_unlisted_email(client, monkeypatch):
    granted: list = []
    monkeypatch.setattr(
        entitlements, "grant_comp", lambda org: granted.append(org) or True
    )
    resp = _callback_login(client, monkeypatch, "stranger@example.com")
    assert resp.status_code == 302
    assert granted == []
