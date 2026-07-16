"""UNASSIGNED actions are a VISIBLE triage state, not a silent dead-end.

The dashboard wire-shape (_action_entry) flags an action the producer couldn't
route (owner="UNASSIGNED"/"") so the UI renders a "⚠ Unassigned" chip with the
gap reason, instead of faint text a reader skips over."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dashboard import _action_entry


def test_explicit_unassigned_is_flagged():
    e = _action_entry({"action_id": "a1", "item": "Book follow-up",
                        "owner": "UNASSIGNED", "gap_type": "owner"})
    assert e["unassigned"] is True
    assert e["gap"] == "owner"


def test_empty_owner_is_unassigned():
    e = _action_entry({"action_id": "a2", "item": "Send deck", "owner": ""})
    assert e["unassigned"] is True


def test_named_owner_is_not_unassigned():
    e = _action_entry({"action_id": "a3", "item": "Send deck", "owner": "Duccio"})
    assert e["unassigned"] is False
    assert e["owner"] == "Duccio"


def test_dashboard_renders_unassigned_chip():
    dashboard = (Path(__file__).resolve().parents[2] / "frontend/dashboard.html").read_text()
    # the triage chip + its helper exist
    assert "function ownerChip(a)" in dashboard
    assert "⚠ Unassigned" in dashboard
    assert ".ow.unassigned" in dashboard
