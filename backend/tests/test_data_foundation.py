"""Data Foundation (DF0-DF1) key-free half; accepted contract v5.

Flag-off inertness (every route 404s, enabled() False), the SourceEnvelope
validator's fail-closed rules, and the network-free fake Drive connector's
Changes-token protocol as pure logic. No database, no vendors.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import datafoundation, store
from app.config import settings
from app.datafoundation import connectors as df_connectors
from app.datafoundation import dal as df_dal
from app.datafoundation.envelope import body_checksum, validate_envelope


def test_disabled_by_default_and_routes_404(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    client = TestClient(main_module.app)
    assert datafoundation.enabled() is False
    assert client.get("/org/data/connectors").status_code == 404
    assert client.post("/org/data/resolve", json={}).status_code == 404
    assert client.get("/dashboard/data/connectors").status_code == 404


def test_flag_alone_is_not_enough_without_control_plane(monkeypatch):
    monkeypatch.setattr(settings, "data_foundation_enabled", True)
    monkeypatch.setattr(settings, "laura_database_url", "")
    assert datafoundation.enabled() is False


def test_envelope_validation_fail_closed():
    # Missing acl_mode NEVER means org_default; it normalizes to unknown.
    clean, errors = validate_envelope({
        "external_id": "x1", "kind": "document", "title": "t",
        "deleted": False, "checksum": "c",
    })
    assert not errors and clean["acl_mode"] == "unknown"
    # mirrored without entries is a quarantine, not a silent default.
    _clean, errors = validate_envelope({
        "external_id": "x1", "kind": "document", "title": "t",
        "deleted": False, "checksum": "c", "acl_mode": "mirrored",
    })
    assert any("acl entries are required" in e for e in errors)
    # Structural failures quarantine.
    for bad in (
        {"kind": "document", "deleted": False},          # no external_id
        {"external_id": "x", "kind": "nope", "deleted": False},
        {"external_id": "x", "kind": "document", "deleted": "yes"},
        "not-an-object",
    ):
        _clean, errors = validate_envelope(bad)
        assert errors, f"{bad!r} must be invalid"
    # The body-pipeline sentinel carries the exact contract reason.
    _clean, errors = validate_envelope({
        "external_id": "x", "kind": "__body_pipeline_disabled__",
        "deleted": False,
    })
    assert "body_pipeline_disabled" in errors


def _fixture() -> dict:
    return {
        "start_token": 1,
        "latest_token": 3,
        "files": {
            "f-open": {"title": "Open doc", "body": "open body",
                       "container": "folder-a", "acl_mode": "org_default"},
            "f-priv": {"title": "Private doc", "body": "secret body",
                       "container": "folder-a", "acl_mode": "mirrored",
                       "acl": [{"principal_kind": "user",
                                "principal_external_id": "alice@x.co",
                                "access": "reader"}]},
            "f-hidden": {"title": "Hidden perms", "body": "??",
                         "permissions_hidden": True},
        },
        "changes": [
            {"token": 2, "file": "f-open", "op": "upsert",
             "body": "open body v2"},
            {"token": 3, "file": "f-priv", "op": "delete"},
        ],
        "identities": [
            {"external_id": "alice@x.co", "kind": "user",
             "display": "Alice", "email": "alice@x.co",
             "email_verified": True},
        ],
    }


def test_fake_gdrive_full_sync_speaks_changes_tokens():
    impl = df_connectors.get("gdrive")
    batch = impl.sync("org-x", {"config_json": {"fixture": _fixture()}}, {},
                      full=True)
    assert batch.new_cursor == {"page_token": 3}
    by_id = {e["external_id"]: e for e in batch.envelopes}
    assert by_id["f-open"]["acl_mode"] == "org_default"
    assert by_id["f-priv"]["acl_mode"] == "mirrored"
    assert by_id["f-priv"]["acl"][0]["principal_external_id"] == "alice@x.co"
    # Unreadable permissions are UNKNOWN; fail-closed, never org_default.
    assert by_id["f-hidden"]["acl_mode"] == "unknown"
    assert by_id["f-open"]["checksum"] == body_checksum("open body")
    assert batch.identities and batch.identities[0]["email_verified"] is True


def test_fake_gdrive_incremental_consumes_only_newer_tokens():
    impl = df_connectors.get("gdrive")
    batch = impl.sync("org-x", {"config_json": {"fixture": _fixture()}},
                      {"page_token": 1}, full=False)
    ids = [(e["external_id"], e["deleted"]) for e in batch.envelopes]
    assert ids == [("f-open", False), ("f-priv", True)]
    assert batch.new_cursor == {"page_token": 3}
    # Fully caught up: nothing to emit, token stays.
    batch = impl.sync("org-x", {"config_json": {"fixture": _fixture()}},
                      {"page_token": 3}, full=False)
    assert batch.envelopes == []


def test_fake_gdrive_expired_token_forces_full_reconciliation():
    fixture = {**_fixture(), "expire_tokens_below": 3}
    impl = df_connectors.get("gdrive")
    batch = impl.sync("org-x", {"config_json": {"fixture": fixture}},
                      {"page_token": 2}, full=False)
    # Token below the expiry floor -> full reconciliation, all files.
    assert {e["external_id"] for e in batch.envelopes} == {
        "f-open", "f-priv", "f-hidden"
    }


def test_fake_gdrive_scope_loss_and_unimplemented_kinds():
    impl = df_connectors.get("gdrive")
    with pytest.raises(df_dal.ScopeLostError):
        impl.sync("org-x",
                  {"config_json": {"fixture": {**_fixture(),
                                               "scope_lost": True}}},
                  {}, full=False)
    with pytest.raises(df_connectors.ConnectorNotImplemented):
        impl.sync("org-x", {"config_json": {}}, {}, full=False)
    for kind in ("slack", "notion", "crm", "custom", "bogus"):
        with pytest.raises(df_connectors.ConnectorNotImplemented):
            df_connectors.get(kind)
