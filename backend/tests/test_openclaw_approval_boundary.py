"""The OpenClaw approval boundary is enforced server-side, not by the prompt.

`run_tool` is reachable by anything holding a run capability token — that
includes the external gateway. The approved-action enum and the filtered plan
Laura sends are prompt data only, so a gateway that ignores them must still be
unable to cause a vendor write. Regression for the audit finding: membership
of a run was accepted as consent.
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store  # noqa: E402
from app.config import settings  # noqa: E402
from app.openclaw import runtime  # noqa: E402


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    monkeypatch.setattr(runtime, "store", store)
    monkeypatch.setattr(settings, "openclaw_experiment_enabled", True)
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", "*")
    runtime._ensure_sqlite_schema()
    return store


ORG = "org-approval"
RUN = "run-approval"
ACTION = "act-approval"


def _seed(status: str) -> None:
    """One run with a single email.send action in the given lifecycle state."""
    now = time.time()
    with store._LOCK, store._connect() as conn:
        conn.execute(
            "INSERT INTO openclaw_runs (org_id, run_id, meeting_id, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (ORG, RUN, "bot-1", "running", now, now),
        )
        conn.execute(
            "INSERT INTO openclaw_action_runs (org_id, run_id, action_id, "
            "status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (ORG, RUN, ACTION, status, now, now),
        )
        conn.commit()


def _call(monkeypatch, tool: str = "gmail_send") -> dict:
    """Drive run_tool exactly as the gateway would, with a valid capability."""
    monkeypatch.setattr(
        runtime, "_action_for_run",
        lambda org, run, action: {
            "action_id": ACTION,
            "typed": {"type": "email.send",
                      "args": {"to": "x@example.test", "subject": "s",
                               "body": "b"}},
        },
    )
    fired: list[str] = []
    monkeypatch.setattr(
        runtime, "_execute_side_effect",
        lambda *a, **k: fired.append("vendor-write") or {"ok": True},
    )
    token = runtime.mint_capability(ORG, RUN)
    result = runtime.run_tool(token, tool,
                              {"action_id": ACTION, "step_id": "step-1"})
    result["_fired"] = fired
    return result


def test_unapproved_action_cannot_drive_a_vendor_write(fresh, monkeypatch):
    """The finding: a gateway returning an action_id that was never approved
    reached the vendor. Membership of the run is not consent."""
    _seed("queued")
    result = _call(monkeypatch)
    assert result["ok"] is False
    assert result["status"] == 403
    assert "not been approved" in result["error"]
    assert result["_fired"] == [], "a vendor write happened without approval"


@pytest.mark.parametrize("status", ["queued", "needs_attention", "rejected",
                                    "failed", "done", ""])
def test_only_running_is_treated_as_approved(fresh, monkeypatch, status):
    """'running' is the state approve_action moves an action into as it
    records the decision and takes the ledger claim. Nothing else counts —
    notably 'done', so a completed action cannot be replayed."""
    _seed(status)
    result = _call(monkeypatch)
    assert result["ok"] is False and result["status"] == 403
    assert result["_fired"] == []


def test_approved_action_still_executes(fresh, monkeypatch):
    """The gate must not break the legitimate path."""
    _seed("running")
    result = _call(monkeypatch)
    assert result["_fired"] == ["vendor-write"], (
        "an approved action must still reach the vendor"
    )


def test_refusal_is_recorded_as_an_event(fresh, monkeypatch):
    """An unapproved attempt is operator-visible, not silently dropped."""
    _seed("queued")
    _call(monkeypatch)
    with store._LOCK, store._connect() as conn:
        kinds = [
            dict(r)["kind"] for r in conn.execute(
                "SELECT kind FROM openclaw_events WHERE org_id=? AND run_id=?",
                (ORG, RUN),
            ).fetchall()
        ]
    assert "tool_rejected" in kinds
