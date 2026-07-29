"""Microsoft Graph connector — key-free connector logic (no DB, no network).

Exercises the connector contract against the deterministic fake Graph tenant:
paged full enumeration, delta incrementals with checkpoints + tombstones,
delta-expiry → full reconciliation, HTTP 429 throttling (raise → the caller
reschedules), 401 / scope loss, inherited + org-wide + fail-closed ACL framing,
nested-group principal emission, and the no-secret persistence invariant. The
Postgres half (commit_batch → visible_heads ACL, identity mapping, revocation,
cross-org isolation) lives in test_graph_connector_pg.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.datafoundation import dal
from app.datafoundation.connectors import ConnectorNotImplemented
from app.datafoundation.graph import (
    FakeGraphTransport,
    GraphConnector,
    GraphThrottled,
)


def _fixture(**overrides) -> dict:
    base = {
        "page_size": 2,
        "start_delta_token": 1,
        "latest_delta_token": 5,
        "items": {
            "doc-open": {"name": "Open doc", "body": "open onboarding body",
                         "container": "site-a", "author": "u-alice",
                         "org_wide": True, "web_url": "https://graph/doc-open",
                         "updated_at": "2026-07-20T10:00:00Z"},
            "doc-hr": {"name": "HR policy", "body": "salary bands",
                       "container": "site-hr", "author": "u-bob",
                       "acl": [{"principal_kind": "group",
                                "principal_external_id": "g-hr",
                                "access": "reader"}]},
            "doc-inherit": {"name": "Team plan", "body": "team notes",
                            "container": "site-a", "inherits_from": "site-a"},
            "doc-secret": {"name": "Sealed", "body": "top secret",
                           "permissions_readable": False},
        },
        "containers": {
            "site-a": {"acl": [{"principal_kind": "group",
                                "principal_external_id": "g-eng",
                                "access": "reader"}]},
        },
        "principals": {
            "users": [
                {"id": "u-alice", "display": "Alice",
                 "email": "alice@acme.test", "email_verified": True},
                {"id": "u-bob", "display": "Bob",
                 "email": "bob@acme.test", "email_verified": True},
            ],
            "groups": [
                {"id": "g-hr", "display": "HR",
                 "members": ["u-bob", "g-leads"]},
                {"id": "g-leads", "display": "Leads", "members": ["u-alice"]},
                {"id": "g-eng", "display": "Eng", "members": ["u-alice"]},
            ],
        },
        "deltas": [
            {"token": 4, "item": "doc-hr", "op": "upsert",
             "body": "updated bands"},
            {"token": 5, "item": "doc-open", "op": "remove"},
        ],
    }
    base.update(overrides)
    return base


def _connector(fixture: dict | None = None) -> dict:
    return {"id": "conn-1", "kind": "graph", "name": "Graph",
            "config_json": {"graph_fixture": fixture or _fixture()}}


def _by_id(envelopes: list[dict]) -> dict[str, dict]:
    return {e["external_id"]: e for e in envelopes}


# ── full enumeration + framing ──────────────────────────────────────────────

def test_full_sync_pages_and_frames_acls():
    batch = GraphConnector().sync("org", _connector(), {}, full=True)
    env = _by_id(batch.envelopes)
    assert set(env) == {"doc-open", "doc-hr", "doc-inherit", "doc-secret"}
    # org-wide → org_default (no per-identity acl rows).
    assert env["doc-open"]["acl_mode"] == "org_default"
    assert env["doc-open"]["acl"] == []
    assert env["doc-open"]["canonical_url"] == "https://graph/doc-open"
    # explicit group grant → mirrored with that entry.
    assert env["doc-hr"]["acl_mode"] == "mirrored"
    assert env["doc-hr"]["acl"] == [
        {"principal_kind": "group", "principal_external_id": "g-hr",
         "access": "reader"}
    ]
    # inherited container grant is resolved into the item's effective ACL.
    assert env["doc-inherit"]["acl_mode"] == "mirrored"
    assert {e["principal_external_id"] for e in env["doc-inherit"]["acl"]} == {
        "g-eng"
    }
    # unreadable permissions FAIL CLOSED → unknown, visible to nobody.
    assert env["doc-secret"]["acl_mode"] == "unknown"
    assert env["doc-secret"]["acl"] == []
    # a full enumeration reports the baseline checkpoint; future deltas advance
    # from there.
    assert batch.new_cursor == {"delta_token": "d1"}


def test_full_sync_emits_nested_group_principals():
    batch = GraphConnector().sync("org", _connector(), {}, full=True)
    ids = {p["external_id"]: p for p in batch.identities}
    assert ids["g-hr"]["kind"] == "group"
    # a group whose members include another group id → nested membership,
    # emitted verbatim for the DAL's closure to resolve.
    assert ids["g-hr"]["members"] == ["u-bob", "g-leads"]
    assert ids["u-alice"]["email"] == "alice@acme.test"
    assert ids["u-alice"]["email_verified"] is True


# ── delta incrementals + tombstones + expiry ────────────────────────────────

def test_delta_incremental_upsert_and_tombstone():
    batch = GraphConnector().sync(
        "org", _connector(), {"delta_token": "d3"}, full=False
    )
    env = _by_id(batch.envelopes)
    assert set(env) == {"doc-hr", "doc-open"}
    # token 4 upsert carries the new body; token 5 is a removal → tombstone.
    assert env["doc-hr"]["deleted"] is False
    assert env["doc-open"]["deleted"] is True
    assert batch.new_cursor == {"delta_token": "d5"}


def test_delta_at_head_is_empty():
    batch = GraphConnector().sync(
        "org", _connector(), {"delta_token": "d5"}, full=False
    )
    assert batch.envelopes == []
    assert batch.new_cursor == {"delta_token": "d5"}


def test_delta_expiry_falls_back_to_full():
    fx = _fixture(expire_tokens_below=99)
    batch = GraphConnector().sync(
        "org", _connector(fx), {"delta_token": "d3"}, full=False
    )
    # An expired token forces a full reconciliation (never a mtime watermark).
    assert set(_by_id(batch.envelopes)) == {
        "doc-open", "doc-hr", "doc-inherit", "doc-secret"
    }


def test_delta_paging_carries_base_across_continuation():
    # page_size 1 forces the change stream to page; both changes must survive
    # the nextLink chain with the correct delta base.
    fx = _fixture(page_size=1)
    batch = GraphConnector().sync(
        "org", _connector(fx), {"delta_token": "d3"}, full=False
    )
    assert set(_by_id(batch.envelopes)) == {"doc-hr", "doc-open"}
    assert batch.new_cursor == {"delta_token": "d5"}


# ── throttling + scope loss ──────────────────────────────────────────────────

def test_429_on_first_page_raises_throttled():
    fx = _fixture(throttle_on_page=0)
    with pytest.raises(GraphThrottled):
        GraphConnector().sync("org", _connector(fx), {}, full=True)


def test_429_mid_drain_raises_and_yields_no_batch():
    # page_size 1 + throttle on the 2nd page: the first page succeeded but the
    # connector never returns a partial batch — it raises, so the caller's run
    # reschedules and the cursor never advances.
    fx = _fixture(page_size=1, throttle_on_page=1)
    with pytest.raises(GraphThrottled):
        GraphConnector().sync("org", _connector(fx), {}, full=True)


def test_401_scope_lost_raises_scopelost():
    fx = _fixture(auth_lost=True)
    with pytest.raises(dal.ScopeLostError):
        GraphConnector().sync("org", _connector(fx), {}, full=True)


# ── injected-transport + fail-closed guards ─────────────────────────────────

def test_missing_fixture_raises_not_implemented():
    with pytest.raises(ConnectorNotImplemented):
        GraphConnector().sync("org", {"id": "c", "kind": "graph",
                                      "config_json": {}}, {}, full=True)


def test_readable_but_no_entries_fails_closed():
    # A readable permission set that resolves to zero grantable principals is
    # 'unknown' (nobody), never org_default (everybody).
    fx = _fixture(items={"doc-empty": {"name": "Empty ACL", "body": "b",
                                       "acl": [], "org_wide": False}},
                  deltas=[], principals={})
    batch = GraphConnector().sync("org", _connector(fx), {}, full=True)
    assert _by_id(batch.envelopes)["doc-empty"]["acl_mode"] == "unknown"


def test_no_secret_or_token_in_output():
    # The connector must never surface credentials in envelopes/identities.
    fx = _fixture()
    fx["access_token"] = "SECRET-do-not-leak"  # a hostile fixture field
    batch = GraphConnector().sync("org", _connector(fx), {}, full=True)
    blob = repr(batch.envelopes) + repr(batch.identities) + repr(
        batch.new_cursor
    )
    assert "SECRET-do-not-leak" not in blob


def test_transport_is_injectable_directly():
    # The transport is a real dependency-injection seam: it can be built and
    # driven without a connector row at all.
    t = FakeGraphTransport(_fixture())
    principals = t.fetch_principals()
    assert any(p["external_id"] == "g-hr" for p in principals)
    page = t.list_items("")
    assert page["items"] and page["next"]  # more than one page
