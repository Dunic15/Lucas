"""The approval refusal is recorded, and every non-approved status refuses.

The gate itself, and the case that matters most (a valid run capability is not
approval), are already covered by
`test_openclaw_connected_apps.test_write_without_approval_is_refused_by_the_tool_bridge`,
which drives a real run through a fake Connect surface and is the stronger
test. This file adds only the two things that one does not assert:

  1. the refusal emits a `tool_rejected` event, so a probe leaves a trace;
  2. the status matrix — EVERY non-`running` state refuses, including `done`,
     so a completed action cannot be re-executed by asking again with a fresh
     step_id.
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

ORG = "org-refusal"
RUN = "run-refusal"
ACTION = "act-refusal"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    monkeypatch.setattr(runtime, "store", store)
    monkeypatch.setattr(settings, "openclaw_experiment_enabled", True)
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", "*")
    runtime._ensure_sqlite_schema()
    return store


def _seed(status: str) -> None:
    now = time.time()
    with store._LOCK, store._connect() as conn:
        conn.execute(
            "INSERT INTO openclaw_runs (org_id, run_id, meeting_id, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (ORG, RUN, "bot-refusal", "running", now, now),
        )
        conn.execute(
            "INSERT INTO openclaw_action_runs (org_id, run_id, action_id, "
            "status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (ORG, RUN, ACTION, status, now, now),
        )
        conn.commit()


def _call(monkeypatch) -> tuple[dict, list[str]]:
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
    result = runtime.run_tool(token, "gmail_send",
                              {"action_id": ACTION, "step_id": "step-1"})
    return result, fired


def test_refusal_emits_a_tool_rejected_event(fresh, monkeypatch):
    _seed("queued")
    result, fired = _call(monkeypatch)
    assert result["ok"] is False and result["status"] == 403
    assert fired == []
    with store._LOCK, store._connect() as conn:
        kinds = [
            dict(r)["kind"] for r in conn.execute(
                "SELECT kind FROM openclaw_events WHERE org_id=? AND run_id=?",
                (ORG, RUN),
            ).fetchall()
        ]
    assert "tool_rejected" in kinds, (
        "a refused write must leave a trace on the run"
    )


@pytest.mark.parametrize("status", ["queued", "needs_attention", "rejected",
                                    "failed", "done", ""])
def test_only_running_is_treated_as_approved(fresh, monkeypatch, status):
    """`done` is in here deliberately: a completed action must not be
    re-executable by asking again under a new step_id."""
    _seed(status)
    result, fired = _call(monkeypatch)
    assert result["ok"] is False and result["status"] == 403
    assert fired == []


def test_approved_action_still_executes(fresh, monkeypatch):
    """The gate must not break the legitimate path."""
    _seed("running")
    _, fired = _call(monkeypatch)
    assert fired == ["vendor-write"]
