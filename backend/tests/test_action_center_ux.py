"""Action Center + Connections UX (owner ask 2026-07-22, live card
aa2e74e86dbd4d38).

Three behaviours under test:

  1. CAPABILITY LIST — every connected tool can say what it actually does, and
     that list is DERIVED from the executor's own mapper (#377), never a second
     hand-kept catalog. If the executor gains/loses a verb, the Connections
     cards, the System Check board and the in-meeting brief all move together.

  2. CARD STATES — a card is never a passive "captured, tracked for the record"
     dead end. Before anything runs it is either ``needs_details`` or
     ``ready_to_approve``; existing tracked-only rows migrate softly on read.
     This is DERIVED, so the canonical execution_status vocabulary (and its
     CHECK constraint) is untouched — no migration.

  3. NEEDS-DETAILS / RECONNECT — an untyped or incomplete card asks for what it
     needs; when the blocker is a missing CONNECTION the card names the tool to
     reconnect instead of dead-ending in a failure.

Key-free like the rest of the suite: sqlite in tmp_path, no vendor touched.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, ledger, store
from app.api import dashboard as dash
from app.brain import tool_registry
from app.config import settings

APP_KEYS = {"slug", "label", "connected", "status", "account", "verbs"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    dash._syscheck_last.clear()
    return TestClient(main_module.app)


def _login(client: TestClient, email: str = "owner@x.com") -> dict:
    user = store.upsert_user(email)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


# ── (1) capability list — derived from the executor, not duplicated ─────────

def test_family_verbs_come_from_the_executor_mapper():
    """The verb list IS the executor's mapper — same source, no second catalog."""
    from app import pipedream_executor as pe

    for slug in ("google_calendar", "gmail", "google_drive", "asana"):
        expected = sorted(
            t.split(".", 1)[1].replace("_", " ")
            for t, spec in pe._MAPPER.items()
            if spec[0] == slug
        )
        assert tool_registry.family_verbs(slug) == expected
        assert expected, f"executor maps no verbs for {slug}"
    # An app the executor does not map claims nothing.
    assert tool_registry.family_verbs("notion") == []


def test_family_verbs_text_is_what_the_meeting_brief_speaks():
    """assemble()'s spoken brief and the dashboard read the same function, so
    the avatar can never claim a verb the boards don't show (#377 regression)."""
    assert tool_registry.family_verbs_text("gmail") == ", ".join(
        tool_registry.family_verbs("gmail")
    )
    assert "create event" in tool_registry.family_verbs_text("google_calendar")


def test_capabilities_endpoint_shape_and_verbs(client, monkeypatch):
    r = client.get("/dashboard/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body.get("generated_at")
    apps = body["apps"]
    assert apps, "the executor maps families, so the list is never empty"
    for a in apps:
        assert APP_KEYS.issubset(a.keys())
        assert isinstance(a["verbs"], list) and a["verbs"]
        assert isinstance(a["connected"], bool)
    slugs = {a["slug"] for a in apps}
    assert {"google_calendar", "gmail", "asana"} <= slugs
    cal = next(a for a in apps if a["slug"] == "google_calendar")
    assert cal["label"] == "Google Calendar"
    assert "create event" in cal["verbs"]


def test_capabilities_endpoint_is_auth_gated(client, monkeypatch):
    """Same four-worlds door as /dashboard/summary: anonymous is 401 when login
    is on (the list reveals which integrations an org has configured)."""
    # Login enabled = a configured Google OAuth client (same lever as syscheck).
    monkeypatch.setattr(settings, "google_calendar_client_id", "gci")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "gcs")
    r = client.get("/dashboard/capabilities")
    assert r.status_code == 401
    assert r.json().get("auth_enabled") is True


def test_capabilities_payload_carries_no_credentials(client, monkeypatch):
    """Distilled only — a verb list and a connection flag, never a token."""
    secret = "SECRET_RT_never_leaks_9z8y"
    # A secret is needed to encrypt the token at rest (fails closed otherwise).
    monkeypatch.setattr(settings, "session_secret", "test-enc-secret")
    store.set_org_oauth(
        settings.demo_org_id, secret, email="acct@x.com", scopes="cal")
    r = client.get("/dashboard/capabilities")
    assert r.status_code == 200
    assert secret not in r.text


def test_syscheck_supports_is_the_executor_verb_list(client, monkeypatch):
    """The System Check board lists real capabilities, not a generic
    ['read','write'] placeholder."""
    rows = {r["key"]: r for r in client.get("/dashboard/syscheck").json()["rows"]}
    assert rows["google_calendar"]["supports"] == tool_registry.family_verbs(
        "google_calendar"
    )
    assert rows["asana"]["supports"] == tool_registry.family_verbs("asana")
    assert "read" not in rows["gmail"]["supports"]


# ── (2) card states — no passive "tracked only" resting state ───────────────

def test_typed_and_complete_card_is_ready_to_approve():
    action = {
        "item": "send the recap",
        "typed": {
            "type": "email.send",
            "args": {"to": ["a@b.c"], "subject": "Recap", "body": "text"},
        },
    }
    assert dash._card_state(action) == "ready_to_approve"


def test_typed_but_incomplete_card_needs_details():
    action = {"item": "send the recap",
              "typed": {"type": "email.send", "args": {"subject": "Recap"}}}
    assert dash._card_state(action) == "needs_details"


def test_untyped_card_needs_details_never_tracked_only():
    """The live failure (card aa2e74e86dbd4d38): an untyped capture used to rest
    as 'captured — tracked here for the record'. It is now actionable."""
    assert dash._card_state({"item": "email Daniel the security checklist"}) == (
        "needs_details"
    )
    assert dash._card_state({"item": "follow up with the vendor"}) == "needs_details"


def test_action_entry_exposes_card_state_and_family_both_branches():
    entry = dash._action_entry({
        "item": "send the recap",
        "typed": {"type": "email.send", "args": {"subject": "x"}},
    })
    assert entry["card_state"] == "needs_details"
    assert entry["family"] == "gmail"
    # the plain-string fallback branch carries the same keys
    bare = dash._action_entry("follow up with the vendor")
    assert bare["card_state"] == "needs_details"
    assert bare["family"] == ""


def test_card_state_never_raises_on_junk():
    for junk in ({}, {"typed": "not-a-dict"}, {"typed": {"type": None}}):
        assert dash._card_state(junk) == "needs_details"


# ── (3) family routing — the "Reconnect X" hint, without false positives ────

@pytest.mark.parametrize(
    "typed_type,expected",
    [
        ("calendar.create_event", "google_calendar"),
        ("calendar.update_event", "google_calendar"),
        ("email.send", "gmail"),
        ("gmail.create_draft", "gmail"),
        ("drive.share_file", "google_drive"),
        ("asana.create_task", "asana"),
    ],
)
def test_typed_family_from_type_prefix(typed_type, expected):
    assert dash._action_family({"item": "x", "typed": {"type": typed_type}}) == expected


def test_untyped_family_only_for_unambiguous_kinds():
    """An unambiguous email/calendar ask names its tool; a generic todo must NOT
    — otherwise a Google-only org would be told to 'Reconnect Asana'."""
    assert dash._action_family({"item": "email Daniel the checklist"}) == "gmail"
    assert dash._action_family(
        {"item": "schedule a follow-up call with Priya next Tuesday"}
    ) == "google_calendar"
    assert dash._action_family({"item": "follow up with the vendor"}) == ""
    assert dash._action_family({"item": ""}) == ""


def test_unknown_typed_family_is_blank_not_guessed():
    assert dash._action_family({"item": "x", "typed": {"type": "slack.post"}}) == ""


# ── the summary path still carries everything the card renders ──────────────

def test_summary_actions_carry_card_state_and_family(client, monkeypatch):
    store.save_artifact(
        "bot-ux-1",
        {
            "summary": "s",
            "org_id": settings.demo_org_id,
            "avatar_id": "laura",
            "meeting_url": "https://meet.google.com/ux-1",
            "actions": [
                {"action_id": "a1", "item": "email Daniel the checklist"},
                {"action_id": "a2", "item": "book the review",
                 "typed": {"type": "calendar.create_event",
                           "args": {"title": "Review", "start": "2026-08-01T10:00:00Z",
                                    "end": "2026-08-01T11:00:00Z"}}},
            ],
        },
    )
    rows = client.get("/dashboard/summary").json().get("meetings") or []
    actions = [a for m in rows for a in (m.get("actions") or [])]
    by_id = {a["action_id"]: a for a in actions if a.get("action_id")}
    assert by_id["a1"]["card_state"] == "needs_details"
    assert by_id["a1"]["family"] == "gmail"
    assert by_id["a2"]["card_state"] == "ready_to_approve"
    assert by_id["a2"]["family"] == "google_calendar"


def test_withdrawal_control_fixture_and_history_bucket():
    """Fixture proof for the visible control and withdrawn-history projection."""
    html = (
        Path(__file__).resolve().parents[2] / "frontend" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert 'data-withdraw="' in html
    assert ">Discard</button>" in html
    assert ">Withdraw</button>" in html
    assert 'withdrawn:"Withdrawn"' in html
    assert 's==="withdrawn") return "done"' in html
    assert '"/withdraw"' in html
    assert "preserved in history" in html
