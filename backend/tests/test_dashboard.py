"""Dashboard API (/dashboard/*) — the owner control view.

Key-free like the rest of the suite: sqlite in tmp_path, no vendors touched.
The critical property under test: the summary endpoint serves DISTILLED data
only — a transcript stored in an artifact must never appear in the response.
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import dashboard, ledger, store
from app.config import settings

SECRET_LINE = "duccio: the acquisition price is nine million"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)  # shares the sqlite file — needs its tables too
    return TestClient(main_module.app)


def _seed_artifact(bot_id: str = "bot_dash_1", avatar_id: str = "laura") -> None:
    store.save_artifact(
        bot_id,
        {
            "summary": "Kickoff went well; sandbox confirmed for day one.",
            "actions": [{"owner": "Ben", "item": "Send the DPA", "done": False}],
            "checklist": [],
            "missing_steps": ["security_approval"],
            "decisions": [{"speaker": "Ben", "decision": "proceed with ACME"}],
            "readiness_score": 78,
            "follow_up_email": {"subject": "ACME kickoff follow-up", "body": "…"},
            "transcript": SECRET_LINE,
            "avatar_id": avatar_id,
            "meeting_url": "https://meet.google.com/dash-test",
            "duration_seconds": 1860,
        },
    )


def test_summary_never_leaks_transcript(client):
    _seed_artifact()
    resp = client.get("/dashboard/summary")
    assert resp.status_code == 200
    assert SECRET_LINE not in resp.text
    assert "transcript" not in resp.json()["meetings"][0]


def test_summary_shape_and_attribution(client):
    _seed_artifact()
    data = client.get("/dashboard/summary").json()

    assert {"avatars", "live", "meetings", "stats", "connections"} <= set(data)

    # Installed avatars come from the registry (laura, cedric, sff, …).
    ids = {a["id"] for a in data["avatars"]}
    assert "laura" in ids and "cedric" in ids

    m = data["meetings"][0]
    assert m["avatar_id"] == "laura"
    assert m["platform"] == "Meet"
    assert m["readiness_score"] == 78
    assert m["actions"][0]["owner"] == "Ben"
    assert m["missing_steps"] == ["security_approval"]
    assert m["follow_up_subject"] == "ACME kickoff follow-up"

    # The rollup attributes the meeting to laura, not cedric.
    laura = next(a for a in data["avatars"] if a["id"] == "laura")
    cedric = next(a for a in data["avatars"] if a["id"] == "cedric")
    assert laura["meetings_total"] == 1
    assert cedric["meetings_total"] == 0
    assert "SFF" not in cedric["role"]
    assert cedric["drive_folder"] is False

    stats = data["stats"]
    assert stats["meetings_30d"] == 1
    assert stats["followups_30d"] == 1
    assert stats["avg_readiness_30d"] == 78
    assert len(stats["weekly"]) == 8 and sum(stats["weekly"]) == 1

    # Connections are booleans only — never secrets.
    assert all(isinstance(v, bool) for v in data["connections"].values())


def test_stats_include_roi_framing(client):
    """The stats block surfaces OUTCOMES (actions executed, follow-ups
    automated, follow-up hours saved), not just note-taking — all derived from
    the real counts, existing keys untouched."""
    _seed_artifact()  # 1 action, 1 follow-up, readiness 78, no execution
    stats = client.get("/dashboard/summary").json()["stats"]
    # Existing keys the frontend reads stay put.
    assert stats["actions_30d"] == 1
    assert stats["followups_30d"] == 1
    # Additive value framing.
    assert stats["followups_automated_30d"] == 1
    assert stats["actions_executed_30d"] == 0  # honest: no dispatch wired yet
    assert stats["roi_minutes_per_action"] == dashboard.FOLLOWUP_MINUTES_SAVED_PER_ACTION
    assert stats["hours_saved_30d"] == round(
        1 * stats["roi_minutes_per_action"] / 60, 1
    )
    assert stats["hours_saved_30d"] > 0


def test_meeting_delivered_summary(client):
    """Each meeting row carries the captured→DELIVERED chips derived from the
    artifact (actions / decisions / follow-up / readiness) — proof Laura
    produced outcomes, not just notes."""
    _seed_artifact()  # 1 action, 1 decision, follow-up subject, readiness 78
    m = client.get("/dashboard/summary").json()["meetings"][0]
    assert "delivered" in m and isinstance(m["delivered"], list)
    assert any("action" in c for c in m["delivered"])
    assert "follow-up email drafted" in m["delivered"]
    assert any("readiness" in c for c in m["delivered"])


def test_summary_includes_live_sessions_without_text(client):
    session = store.create("bot_live_1", "https://zoom.us/j/123", avatar_id="cedric")
    session.add_utterance("Ben", SECRET_LINE)
    try:
        data = client.get("/dashboard/summary").json()
        live = data["live"]
        assert len(live) == 1
        assert live[0]["avatar_id"] == "cedric"
        assert live[0]["platform"] == "Zoom"
        assert live[0]["utterances"] == 1  # count only
        assert SECRET_LINE not in str(data)
        cedric = next(a for a in data["avatars"] if a["id"] == "cedric")
        assert cedric["live_now"] == 1
    finally:
        store.remove("bot_live_1")


def test_summary_respects_bearer_gate(client, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    assert client.get("/dashboard/summary").status_code == 401
    ok = client.get(
        "/dashboard/summary", headers={"Authorization": "Bearer sesame"}
    )
    assert ok.status_code == 200


def test_hidden_avatar_excluded_and_enriched(client):
    """sff is a knowledge pack (hidden: true) — not a callable avatar, so it must
    NOT appear in the dashboard list; Laura/Cedric must, with capabilities."""
    _seed_artifact()
    data = client.get("/dashboard/summary").json()
    ids = {a["id"] for a in data["avatars"]}
    assert "sff" not in ids
    assert {"laura", "cedric"} <= ids
    assert "duccio" not in ids

    laura = next(a for a in data["avatars"] if a["id"] == "laura")
    assert isinstance(laura["capabilities"], list) and laura["capabilities"]
    assert "knowledge_topics" in laura and "process_templates" in laura
    assert "minutes_total" in laura


def test_public_avatar_picker_is_customer_roster_only(client):
    ids = {row["id"] for row in client.get("/avatars").json()["avatars"]}
    assert ids == {"laura", "cedric"}


def test_avatar_email_default_bare_others_tagged(client):
    """The watched inbox IS the default avatar's address (bare); every other
    avatar is a +tag alias of it. An untagged invite falls back to the default
    avatar, so the bare address genuinely summons it."""
    data = client.get("/dashboard/summary").json()
    by_id = {a["id"]: a for a in data["avatars"]}
    raw = settings.calendar_invite_emails.split(",")[0].strip()
    local, _, domain = raw.partition("@")
    base = local.split("+")[0]
    assert by_id[settings.default_avatar_id]["email"] == f"{base}@{domain}"
    other = next(i for i in by_id if i != settings.default_avatar_id)
    assert by_id[other]["email"] == f"{base}+{other}@{domain}"


def test_billing_block_real_minutes(client):
    _seed_artifact()  # duration_seconds = 1860 => 31 min
    b = client.get("/dashboard/summary").json()["billing"]
    assert b["total_minutes"] == 31
    assert b["billing_live"] is False
    assert b["est_cost_30d"] == round(31 * b["rate_per_min"], 2)


def test_dashboard_page_served(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Laura — Dashboard" in resp.text


def test_dashboard_is_customer_facing_and_enterprise_is_honest(client):
    html = client.get("/dashboard").text
    assert "/laura-reference.jpg?avatar_id=" in html
    assert 'loading="lazy"' in html
    assert 'aria-live="polite"' in html
    assert "onerror=" not in html
    assert "SELF-SERVE WIZARD IS ON THE ROADMAP" not in html
    assert "Estimated cost" not in html
    assert "15 min" in html
    assert "Enterprise" in html and "Early access" in html
    assert "Enterprise ready" not in html
    # Collection handlers must use querySelectorAll ($), not querySelector ($).
    # A single Element has no forEach and would break nav/disconnect at runtime.
    assert '    $("#nav button").forEach' not in html
    assert '    $("#av-grid [data-brain-off]").forEach' not in html


def test_dashboard_rejects_non_meeting_links_before_dispatch(client):
    html = client.get("/dashboard").text
    assert "meet\\.google\\.com" in html
    assert "zoom\\.us" in html
    assert "teams\\.(microsoft\\.com|live\\.com)" in html
    assert "Paste a Google Meet, Zoom or Microsoft Teams meeting link." in html


def test_legacy_artifact_without_avatar_id(client):
    """Artifacts saved before avatar_id stamping still render (attributed to
    the empty id, not crashed on)."""
    store.save_artifact("bot_legacy", {"summary": "old one", "actions": []})
    data = client.get("/dashboard/summary").json()
    m = next(x for x in data["meetings"] if x["bot_id"] == "bot_legacy")
    assert m["avatar_id"] == ""
    assert m["platform"] == "—"
    assert m["duration_seconds"] == 0
