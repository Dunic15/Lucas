"""A session-shaped personal org (``u_<hash>``) must never 500 billing or
artifact paths.

The durable control plane keys on UUIDs; personal identities are ``u_<hash>``
cache keys. Every durable query casts org_id to uuid (directly or via the RLS
``app.current_org::uuid`` pin), so a personal org reaching one crashes with a
cast error that surfaced as a 500 loop on /billing/summary and dashboard
artifact reads. These tests pin the degrade path: personal orgs get free-tier
defaults / the SQLite store, and never touch a Postgres engine at all.
"""
from __future__ import annotations

import pytest

from app import billing, control_plane, entitlements, store

PERSONAL_ORG = "u_3f9a1c"  # session-shaped identity. NOT a uuid


def _explode(*a, **k):  # any engine touch for a personal org IS the old bug
    raise AssertionError("personal org must never reach a Postgres engine")


@pytest.fixture
def durable_on(monkeypatch):
    """Control plane enabled, but any engine access explodes the test."""
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(control_plane, "_get_engine", _explode)
    monkeypatch.setattr(entitlements, "_engine", _explode)


def test_usage_summary_degrades_for_personal_org(durable_on):
    assert entitlements.usage_summary(PERSONAL_ORG) is None


def test_get_billing_degrades_for_personal_org(durable_on):
    assert control_plane.get_billing(PERSONAL_ORG) is None


def test_billing_summary_is_free_tier_not_500(durable_on, monkeypatch):
    """The endpoint's worker: a personal org yields the free-tier summary -
    before the fix this raised out of usage_summary/get_billing (a 500)."""
    data = billing._summary_for_org(PERSONAL_ORG, None)
    assert data["plan"] == "free"
    assert data["billing_live"] in (True, False)  # shape intact
    assert data["remaining_seconds"] >= 0


def test_artifact_save_and_get_stay_sqlite_for_personal_org(durable_on, monkeypatch):
    """With durable artifacts on, a personal org's artifact round-trips through
    SQLite only; the durable path (uuid cast) is never invoked."""
    monkeypatch.setattr(store, "durable_artifacts_enabled", lambda: True)
    monkeypatch.setattr(control_plane, "save_artifact", _explode)
    monkeypatch.setattr(control_plane, "get_artifact", _explode)

    bot_id = "bot-personal-org-test"
    artifact = {"org_id": PERSONAL_ORG, "summary": "s"}
    try:
        store.save_artifact(bot_id, artifact, org_id=PERSONAL_ORG)
        got = store.get_artifact(bot_id, org_id=PERSONAL_ORG)
        assert got is not None and got.get("summary") == "s"
    finally:
        store._artifacts.pop(bot_id, None)


def test_durable_org_still_takes_postgres_path(monkeypatch):
    """Guard must not over-block: a real uuid org still routes durable."""
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    calls = {}
    monkeypatch.setattr(store, "durable_artifacts_enabled", lambda: True)
    monkeypatch.setattr(
        control_plane, "get_artifact", lambda o, b: calls.setdefault("hit", (o, b))
    )
    uuid_org = "bf4a683b-0000-4000-8000-000000000000"
    store.get_artifact("bot-x", org_id=uuid_org)
    assert calls["hit"] == (uuid_org, "bot-x")
