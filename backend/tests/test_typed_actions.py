"""Typed-action producer (brain.type_actions) — the finalize-time step that
turns free-text actions into native-executor specs where they CLEARLY map.

Key-free: forced onto the stub post-provider (deterministic regex mapping), so
this exercises the exact path the offline demo runs. The mechanical
no-invented-recipients guard (_sanitize_typed) is tested directly against a
hallucinated recipient, so the "never invent" rule holds even for the model
path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import engine as brain  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_post(monkeypatch):
    # Force the deterministic (key-free) mapping path.
    monkeypatch.setattr(settings, "brain_provider", "stub")
    monkeypatch.setattr(settings, "brain_provider_post", "")
    assert brain.post_provider() == "stub"


def _typed(action: dict) -> dict | None:
    return brain.type_actions([action])[0].get("typed")


# ── the two mappings that fire ──

def test_email_action_maps_with_grounded_recipient():
    typed = _typed({"item": "Email the recap to marco@acme.com", "owner": "Ben"})
    # With no owner domain known, draft-first (default ON) downgrades the
    # send to a Gmail draft — the recipient can't be proven internal.
    assert typed["type"] == "email.draft"
    assert typed["args"]["to"] == ["marco@acme.com"]
    assert typed["args"]["subject"]  # derived from the item, non-empty


def test_email_action_stays_send_for_internal_recipient():
    typed = brain.type_actions(
        [{"item": "Email the recap to marco@acme.com", "owner": "Ben"}],
        owner_email="ben@acme.com",
    )[0].get("typed")
    assert typed["type"] == "email.send"


def test_calendar_action_maps_with_two_iso_datetimes():
    typed = _typed(
        {
            "item": "Schedule a follow-up call 2026-08-01T15:00:00 to "
            "2026-08-01T15:30:00 with dana@acme.com",
            "owner": "",
        }
    )
    assert typed["type"] == "calendar.create_event"
    assert typed["args"]["start"] == "2026-08-01T15:00:00"
    assert typed["args"]["end"] == "2026-08-01T15:30:00"
    assert typed["args"]["attendees"] == ["dana@acme.com"]


# ── everything else stays generic (precision over recall) ──

@pytest.mark.parametrize(
    "item",
    [
        "Email the recap",              # email intent, no address → never invent
        "Send the report to the team",  # no address at all
        "Schedule a follow-up",         # calendar intent, no time → never invent
        "Review the Q3 budget",         # neither intent
    ],
)
def test_unmappable_actions_stay_generic(item):
    out = brain.type_actions([{"item": item, "owner": "Ana", "action_id": "x"}])
    assert "typed" not in out[0]


def test_non_dict_actions_pass_through_unchanged():
    out = brain.type_actions(["a bare string", {"item": "Review the budget"}])
    assert out[0] == "a bare string"
    assert "typed" not in out[1]


def test_empty_and_none_are_safe():
    assert brain.type_actions([]) == []
    assert brain.type_actions(None) == []


def test_returns_a_new_list_without_mutating_input():
    original = {"item": "Email the recap to marco@acme.com"}
    out = brain.type_actions([original])
    assert "typed" in out[0]
    assert "typed" not in original  # producer copies before annotating


# ── the mechanical "never invent a recipient" guard (covers the model path) ──

def test_sanitize_rejects_ungrounded_recipient():
    # Simulates a model hallucinating a recipient not present in the source.
    action = {"item": "Email marco@acme.com the recap"}
    bad = brain._sanitize_typed(
        {"type": "email.send", "args": {"to": ["evil@attacker.com"], "subject": "s"}},
        action,
    )
    assert bad is None
    good = brain._sanitize_typed(
        {"type": "email.send", "args": {"to": ["marco@acme.com"], "subject": "s"}},
        action,
    )
    assert good["args"]["to"] == ["marco@acme.com"]


def test_sanitize_rejects_calendar_without_iso_times():
    action = {"item": "Schedule a call"}
    assert (
        brain._sanitize_typed(
            {"type": "calendar.create_event",
             "args": {"title": "Call", "start": "next Friday", "end": "later"}},
            action,
        )
        is None
    )


def test_sanitize_rejects_unknown_type():
    assert brain._sanitize_typed({"type": "slack.post", "args": {}}, {"item": "x"}) is None
