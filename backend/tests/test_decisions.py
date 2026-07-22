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
def test_dashboard_decision_entries_projection_pipeline_shaped():
    """The CONSUMED path, exercised with a record shaped the way the REAL
    pipeline emits it (engine._build_decision_records → decision/maker/reason/
    project ONLY, never supersedes/status). The projection must still be clean:
    status defaults to 'active', the supersede link is empty (the artifact
    snapshot cannot carry one), and no transcript leaks."""
    from app.api import dashboard

    state = brain.meeting_state.MeetingState()
    records = brain._build_decision_records(
        [
            {
                "decision": "Adopt Postgres",
                "decision_maker": "Priya",
                "reason": "RLS",
                "related_project": "Data",
            }
        ],
        ["Adopt Postgres"],
        state,
    )
    # Guard the premise: the real pipeline shape has NO supersede/status keys.
    assert "supersedes" not in records[0]
    assert "status" not in records[0]

    art = {
        "decision_records": records,
        "decisions": ["Adopt Postgres"],
        "transcript": "SECRET TRANSCRIPT TEXT",
    }
    entries = dashboard._decision_entries(art)
    assert entries[0]["decision"] == "Adopt Postgres"
    assert entries[0]["decision_maker"] == "Priya"
    assert entries[0]["related_project"] == "Data"
    # defaulted by the projection — the artifact record never carried these
    assert entries[0]["status"] == "active"
    assert entries[0]["supersedes"] == ""
    # distilled only — no transcript leaks through the projection
    assert "SECRET" not in json.dumps(entries)


def test_dashboard_decision_entries_reconciled_supersede_flows(isolated_store):
    """The DEFENSIBLE differentiator — the flipped status + supersede link —
    reaches the projection ONLY through the reconciled store rows
    (store.list_decisions), which is exactly the path _meeting_row now feeds in
    via ``db_records``. The artifact's own decision_records can never surface
    the pill (previous test), so this proves the real end-to-end flow."""
    store = isolated_store
    from app.api import dashboard

    old_id = store.save_decision(
        ORG, "bot_meet",
        {"decision": "Go with MySQL", "related_project": "DB choice"},
    )
    store.persist_decision_records(
        ORG,
        "bot_meet",
        [
            {
                "decision": "We're superseding the MySQL call and moving to Postgres",
                "decision_maker": "Priya",
                "related_project": "DB choice",
            }
        ],
    )
    # The reconciled rows the dashboard actually fetches for this meeting.
    db_records = store.list_decisions(ORG, "bot_meet")
    entries = dashboard._decision_entries({"decisions": []}, db_records=db_records)

    # the older decision flipped to 'superseded' (renders "(superseded)")
    assert "superseded" in {e["status"] for e in entries}
    # the newer decision carries the supersede LINK the pill renders from
    superseding = [e for e in entries if e["supersedes"]]
    assert superseding and superseding[0]["supersedes"] == old_id


def test_dashboard_decision_entries_legacy_fallback():
    from app.api import dashboard

    # pre-0017 artifact: only list[str] decisions, no decision_records
    entries = dashboard._decision_entries({"decisions": ["Ship on Friday"]})
    assert len(entries) == 1
    assert entries[0]["decision"] == "Ship on Friday"
    assert entries[0]["decision_maker"] == ""


# ── HTTP: the dedicated decisions endpoint (auth gate + per-user scope) ────
@pytest.fixture
def http_client(tmp_path, monkeypatch):
    """A TestClient over a fresh key-free store — the same reload dance the
    other dashboard HTTP suites use so ledger shares the sqlite file."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "http.sqlite3"))
    import app.main as main_module
    from app import ledger as ledger_mod
    from app import store as store_mod

    importlib.reload(store_mod)
    importlib.reload(ledger_mod)
    return TestClient(main_module.app), store_mod


def _login(client, store_mod, email, name="Test User") -> dict:
    from app import auth

    user = store_mod.upsert_user(email=email, name=name)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def _http_decision_artifact(org_id: str, summary: str, transcript: str) -> dict:
    return {
        "summary": summary,
        "actions": [],
        "readiness_score": 50,
        "follow_up_email": {},
        "avatar_id": "laura",
        "org_id": org_id,
        "meeting_url": "https://meet.google.com/x",
        "transcript": transcript,
        "decision_records": [
            {"decision": "Adopt Postgres", "decision_maker": "Priya",
             "reason": "RLS", "related_project": "Data"}
        ],
        "decisions": ["Adopt Postgres"],
    }


def test_decisions_endpoint_per_user_archive_scope(http_client):
    """A cookie user reads decisions only for meetings they attended
    (transcript speaker) or dispatched (principal_id) — even inside their own
    shared org. A teammate-only meeting in the same org returns 404, exactly
    like /dashboard/summary hides it."""
    client, store_mod = http_client
    alice = _login(client, store_mod, "alice@example.com", name="Ananth Iyer")
    org = alice["org_id"]

    store_mod.save_artifact(
        "bot_att",
        _http_decision_artifact(org, "attended", "Ananth Iyer: kickoff\nlaura: noted"),
    )
    dispatched = _http_decision_artifact(org, "dispatched", "Duccio Profeti: hi")
    dispatched["principal_id"] = alice["user_id"]
    store_mod.save_artifact("bot_disp", dispatched)
    store_mod.save_artifact(
        "bot_mate",
        _http_decision_artifact(org, "teammate only", "Duccio Profeti: solo test"),
    )

    # attendee sees the reconciled records
    r = client.get("/dashboard/meetings/bot_att/decisions")
    assert r.status_code == 200
    body = r.json()
    assert body["bot_id"] == "bot_att"
    assert any(d["decision"] == "Adopt Postgres" for d in body["decisions"])

    # dispatcher (principal_id) sees them too
    assert client.get("/dashboard/meetings/bot_disp/decisions").status_code == 200

    # same org, but Alice neither attended nor dispatched → 404 (absence)
    r404 = client.get("/dashboard/meetings/bot_mate/decisions")
    assert r404.status_code == 404
    assert "Adopt Postgres" not in r404.text


def test_decisions_endpoint_cross_org_isolation(http_client):
    """A meeting in another tenant is invisible at the HTTP layer — 404, never
    the records."""
    client, store_mod = http_client
    _login(client, store_mod, "alice@example.com", name="Alice")
    bob = store_mod.upsert_user(email="bob@example.com", name="Bob")
    store_mod.save_artifact(
        "bot_bob",
        _http_decision_artifact(bob["org_id"], "bob meeting", "Bob: hi"),
    )
    assert client.get("/dashboard/meetings/bot_bob/decisions").status_code == 404
