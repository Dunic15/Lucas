"""First-class Decision records (0017 / meeting_decisions) — SQLite path.

Covers the key-free/demo persistence path and the finalize hook:

* the DAL (save_decision / list_decisions / get_decision / mark_superseded)
* persist_decision_records wires a supersede link (old row flips, never two
  'active' on the same project)
* the post-meeting artifact grows a structured ``decision_records`` list while
  ``decisions`` STAYS list[str] (backward compat) — downstream count/search
  keep working
* save_artifact persists the records and is idempotent on a finalize retry

No network calls, no API keys, no decision/transcript text logged.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars
from app.brain import engine as brain
from app.config import settings


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    original = os.environ.get("LAURA_STORE_PATH")
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))

    import app.store as store  # noqa: WPS433

    store = importlib.reload(store)
    yield store

    if original is None:
        monkeypatch.delenv("LAURA_STORE_PATH", raising=False)
    else:
        monkeypatch.setenv("LAURA_STORE_PATH", original)
    importlib.reload(store)


ORG = "org-decisions-test"


# ── DAL round-trip ────────────────────────────────────────────────────────
def test_save_list_get_decision_roundtrip(isolated_store):
    store = isolated_store
    did = store.save_decision(
        ORG,
        "bot_1",
        {
            "decision": "Adopt Postgres for the control plane",
            "decision_maker": "Priya",
            "reason": "RLS gives us tenant isolation for free",
            "related_project": "Data platform",
        },
    )
    assert did

    rows = store.list_decisions(ORG, "bot_1")
    assert len(rows) == 1
    row = rows[0]
    assert row["decision"] == "Adopt Postgres for the control plane"
    assert row["decision_maker"] == "Priya"
    assert row["reason"] == "RLS gives us tenant isolation for free"
    assert row["related_project"] == "Data platform"
    assert row["status"] == "active"
    assert row["supersedes"] in (None, "")

    got = store.get_decision(ORG, did)
    assert got is not None and got["id"] == did

    # tenancy: another org sees nothing
    assert store.list_decisions("some-other-org", "bot_1") == []


def test_empty_decision_is_not_saved(isolated_store):
    store = isolated_store
    assert store.save_decision(ORG, "bot_x", {"decision": "   "}) is None
    assert store.list_decisions(ORG, "bot_x") == []


def test_mark_superseded_flips_status_keeps_row(isolated_store):
    store = isolated_store
    did = store.save_decision(
        ORG, "bot_1", {"decision": "Use MySQL", "related_project": "DB"}
    )
    assert store.mark_superseded(ORG, did) is True
    got = store.get_decision(ORG, did)
    assert got["status"] == "superseded"  # row kept, not deleted


# ── supersede linking ─────────────────────────────────────────────────────
def test_persist_records_links_supersede_and_flips_old(isolated_store):
    store = isolated_store
    old_id = store.save_decision(
        ORG,
        "bot_old",
        {"decision": "Go with MySQL", "related_project": "Database choice"},
    )

    new_ids = store.persist_decision_records(
        ORG,
        "bot_new",
        [
            {
                "decision": "We're superseding the MySQL call and moving to Postgres",
                "decision_maker": "Priya",
                "related_project": "Database choice",
            }
        ],
    )
    assert len(new_ids) == 1
    new_id = new_ids[0]

    new_row = store.get_decision(ORG, new_id)
    old_row = store.get_decision(ORG, old_id)
    assert new_row["supersedes"] == old_id
    assert new_row["status"] == "active"
    # never two 'active' on the same link
    assert old_row["status"] == "superseded"


def test_persist_records_no_cue_leaves_prior_active(isolated_store):
    store = isolated_store
    old_id = store.save_decision(
        ORG, "bot_old", {"decision": "Ship on Friday", "related_project": "Launch"}
    )
    store.persist_decision_records(
        ORG,
        "bot_new",
        # same project but NO supersede cue → not a supersede
        [{"decision": "Add a status page", "related_project": "Launch"}],
    )
    assert store.get_decision(ORG, old_id)["status"] == "active"


def test_persist_records_within_batch_supersede(isolated_store):
    """A later record in the same batch can supersede an earlier one."""
    store = isolated_store
    ids = store.persist_decision_records(
        ORG,
        "bot_batch",
        [
            {"decision": "Pick vendor A", "related_project": "Vendor"},
            {
                "decision": "Instead of vendor A, go with vendor B",
                "related_project": "Vendor",
            },
        ],
    )
    assert len(ids) == 2
    first, second = store.get_decision(ORG, ids[0]), store.get_decision(ORG, ids[1])
    assert first["status"] == "superseded"
    assert second["supersedes"] == ids[0]


# ── finalize path: decision_records + backward compat ─────────────────────
def _decision_transcript() -> str:
    return (
        "Priya: I think we've decided to go with Postgres for the control plane.\n"
        "Marco: Agreed, that's the decision then.\n"
        "Priya: Great, we'll go with Postgres."
    )


def test_stub_post_meeting_produces_decision_records_with_maker(monkeypatch):
    """Key-free stub path: decision_records carry a decision_maker backfilled
    from the silent tracker's speaker, and decisions STAYS list[str]."""
    monkeypatch.setattr(settings, "brain_provider_post", "stub")
    artifact = brain.post_meeting(avatars.load("laura"), _decision_transcript())

    # backward compat: decisions is STILL list[str]
    assert isinstance(artifact["decisions"], list)
    assert artifact["decisions"], "expected the tracker to capture a decision"
    assert all(isinstance(d, str) for d in artifact["decisions"])

    # new: structured records exist, each with a maker backfilled from tracker
    records = artifact["decision_records"]
    assert isinstance(records, list) and records
    assert all(isinstance(r, dict) for r in records)
    assert all("decision" in r and isinstance(r["decision"], str) for r in records)
    assert any(r["decision_maker"] for r in records), "maker should be backfilled"

    # downstream compat: the count consumers use still works on list[str]
    assert len(artifact["decisions"]) == len([d for d in artifact["decisions"]])
    # org_api-style str(d) search over the list[str] never explodes
    assert " ".join(str(d) for d in artifact["decisions"])


def test_model_decision_records_preserved_and_list_str_kept(monkeypatch):
    """When the model emits both, decisions stays list[str] and the structured
    records ride alongside."""
    model_artifact = {
        "summary": "The team locked the datastore choice.",
        "decisions": ["Adopt Postgres for the control plane"],
        "decision_records": [
            {
                "decision": "Adopt Postgres for the control plane",
                "decision_maker": "Priya",
                "reason": "tenant isolation via RLS",
                "related_project": "Data platform",
            }
        ],
        "actions": [],
        "risks": [],
        "follow_up_email": {},
    }
    monkeypatch.setattr(settings, "brain_provider_post", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(brain.llm, "complete", lambda s, u, **k: json.dumps(model_artifact))
    monkeypatch.setattr(brain, "retrieve", lambda avatar, query, k=6: [])

    artifact = brain.post_meeting(avatars.load("laura"), _decision_transcript())

    assert artifact["decisions"] == ["Adopt Postgres for the control plane"]
    assert all(isinstance(d, str) for d in artifact["decisions"])
    rec = artifact["decision_records"]
    assert rec and rec[0]["decision_maker"] == "Priya"
    assert rec[0]["related_project"] == "Data platform"


def test_prompt_asks_for_decision_records(monkeypatch):
    seen = {}
    model_artifact = {"summary": "x", "decisions": [], "actions": [],
                      "risks": [], "follow_up_email": {}}

    def fake_complete(system, user, **kwargs):
        seen["system"] = system
        return json.dumps(model_artifact)

    monkeypatch.setattr(settings, "brain_provider_post", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(brain.llm, "complete", fake_complete)
    monkeypatch.setattr(brain, "retrieve", lambda avatar, query, k=6: [])
    brain.post_meeting(avatars.load("laura"), "Priya: nothing decided today.")
    assert "decision_records" in seen["system"]


# ── save_artifact persists records + idempotency ──────────────────────────
def test_save_artifact_persists_decision_records(isolated_store):
    store = isolated_store
    artifact = {
        "summary": "s",
        "decisions": ["Adopt Postgres"],
        "decision_records": [
            {"decision": "Adopt Postgres", "decision_maker": "Priya",
             "reason": "", "related_project": "Data"}
        ],
        "org_id": ORG,
    }
    store.save_artifact("bot_fin", artifact, org_id=ORG)
    rows = store.list_decisions(ORG, "bot_fin")
    assert len(rows) == 1
    assert rows[0]["decision"] == "Adopt Postgres"
    assert rows[0]["decision_maker"] == "Priya"

    # idempotent: a finalize retry must not double-insert
    store.save_artifact("bot_fin", artifact, org_id=ORG)
    assert len(store.list_decisions(ORG, "bot_fin")) == 1


def test_save_artifact_without_records_is_noop(isolated_store):
    store = isolated_store
    store.save_artifact("bot_none", {"summary": "s", "decisions": []}, org_id=ORG)
    assert store.list_decisions(ORG, "bot_none") == []


# ── dashboard projection: distilled, never transcript ─────────────────────
def test_dashboard_decision_entries_projection():
    from app.api import dashboard

    art = {
        "decision_records": [
            {"decision": "Adopt Postgres", "decision_maker": "Priya",
             "reason": "RLS", "related_project": "Data", "supersedes": "abc",
             "status": "active"}
        ],
        "decisions": ["Adopt Postgres"],
        "transcript": "SECRET TRANSCRIPT TEXT",
    }
    entries = dashboard._decision_entries(art)
    assert entries[0]["decision"] == "Adopt Postgres"
    assert entries[0]["decision_maker"] == "Priya"
    assert entries[0]["supersedes"] == "abc"
    # distilled only — no transcript leaks through the projection
    assert "SECRET" not in json.dumps(entries)


def test_dashboard_decision_entries_legacy_fallback():
    from app.api import dashboard

    # pre-0017 artifact: only list[str] decisions, no decision_records
    entries = dashboard._decision_entries({"decisions": ["Ship on Friday"]})
    assert len(entries) == 1
    assert entries[0]["decision"] == "Ship on Friday"
    assert entries[0]["decision_maker"] == ""
