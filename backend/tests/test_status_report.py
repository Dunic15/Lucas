"""Laura's weekly status report: composer, RAG heuristic, flag gating, and
the dashboard endpoint's 404-when-off surface. Key-free — the memory window
is monkeypatched, no Postgres."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import settings
from app.memory import meeting_memory, status_report

ORG = "00000000-0000-0000-0000-0000000000de"


def _row(day="2026-07-29", summary="Sprint review.", decisions='["Ship Aug 15"]',
         risks="[]", actions="[]"):
    return {
        "bot_id": "b1", "meeting_key": "k1", "meeting_type": "standup",
        "summary": summary, "decisions_json": decisions, "risks_json": risks,
        "actions_json": actions, "ended_at": "", "day": day,
    }


@pytest.fixture
def _on(monkeypatch):
    monkeypatch.setattr(settings, "status_report_enabled", True)
    monkeypatch.setattr(meeting_memory, "enabled", lambda: True)

    class _Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        status_report.meeting_memory, "_engine",
        lambda: SimpleNamespace(begin=lambda: _Ctx()),
    )
    monkeypatch.setattr(status_report.meeting_memory, "_set_org", lambda c, o: None)
    return monkeypatch


def _window(monkeypatch, rows):
    monkeypatch.setattr(
        status_report.meeting_memory, "_window_rows", lambda c, o, a: rows
    )


# ── gating ─────────────────────────────────────────────────────────────────

def test_flag_off_by_default_and_composer_inert():
    assert settings.status_report_enabled is False
    assert status_report.enabled() is False
    out = status_report.compose(ORG)
    assert out["ok"] is False


def test_endpoint_404_when_flag_off():
    client = TestClient(main_module.app)
    assert client.get("/dashboard/status-report").status_code == 404


# ── composer ───────────────────────────────────────────────────────────────

def test_compose_builds_the_one_pager(_on):
    _window(_on, [_row(
        risks='["Vendor SSO is blocked"]',
        actions='[{"item": "Send the DPA", "owner": "Dana", "deadline": "Friday"}]',
    )])
    out = status_report.compose(ORG, "laura")
    assert out["ok"] is True and out["source_meetings"] == 1
    rep = out["report"]
    assert rep["rag"] == "red"  # "blocked" in a risk line
    assert "1 meeting" in rep["exec_summary"]
    assert rep["done"] and rep["done"][0].startswith("2026-07-29 [standup]")
    assert rep["risks"] == ["Vendor SSO is blocked"]
    assert rep["decisions_needed"] == ["Ship Aug 15"]
    assert rep["actions_open"][0]["owner"] == "Dana"
    assert rep["upcoming"][0].startswith("Send the DPA — Dana, due Friday")


@pytest.mark.parametrize("risks,actions,want", [
    ("[]", '[{"item": "x", "owner": "Dana"}]', "green"),
    ('["Budget overrun possible"]', "[]", "amber"),
    ("[]", '[{"item": "x", "owner": "UNASSIGNED"}]', "amber"),
    ('["The launch slipped a week"]', "[]", "red"),
])
def test_rag_heuristic(_on, risks, actions, want):
    _window(_on, [_row(risks=risks, actions=actions)])
    assert status_report.compose(ORG)["report"]["rag"] == want


def test_compose_empty_window_soft_fails(_on):
    _window(_on, [])
    out = status_report.compose(ORG)
    assert out["ok"] is False and "no meetings" in out["error"]


def test_compose_never_raises(_on):
    def _boom(c, o, a):
        raise RuntimeError("window exploded")

    _on.setattr(status_report.meeting_memory, "_window_rows", _boom)
    out = status_report.compose(ORG)
    assert out["ok"] is False and "RuntimeError" in out["error"]
