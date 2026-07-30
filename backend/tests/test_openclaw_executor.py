"""OpenClaw action route — flag gating, CLI transport parsing, and executor
dispatch + ledger provenance when approved actions run through the local
OpenClaw gateway agent instead of the native vendor clients.

Everything here is key-free and offline: the openclaw CLI is mocked at the
subprocess seam, flags are toggled per test, and both flags stay OFF by
default so the rest of the suite (and the demo) is untouched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store  # noqa: E402
from app.config import settings  # noqa: E402
from app.actions import executor, openclaw_executor  # noqa: E402


def _fresh_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()


class _Proc:
    def __init__(self, stdout: str, rc: int = 0):
        self.stdout, self.returncode, self.stderr = stdout, rc, ""


def _cli_json(reply_text: str, status: str = "ok") -> str:
    """A faithful `openclaw agent --json` stdout envelope."""
    return json.dumps({
        "runId": "r1", "status": status, "summary": "completed",
        "result": {
            "payloads": [{"text": reply_text, "mediaUrl": None}],
            "meta": {"finalAssistantVisibleText": reply_text},
        },
    })


def _mock_cli(monkeypatch, reply_text: str, status: str = "ok") -> list:
    calls: list = []

    def fake_run(cmd, capture_output=True, text=True, timeout=0):
        calls.append(cmd)
        return _Proc(_cli_json(reply_text, status))

    monkeypatch.setattr(openclaw_executor.subprocess, "run", fake_run)
    return calls


# ── flag gating ──

def test_flag_off_keeps_native_route(monkeypatch):
    monkeypatch.setattr(settings, "openclaw_executor", False)
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(executor.ledger, "set_action_status", lambda *a, **k: True)
    monkeypatch.setattr(
        executor.google_client, "send_gmail",
        lambda org, msg: {"ok": True, "message_id": "m1"},
    )
    oc_calls = _mock_cli(monkeypatch, "{}")
    r = executor.execute_approved("org-a", "a1", {"type": "email.send", "message": {}})
    assert r["ok"] is True and oc_calls == []


def test_handles_when_only_openclaw_on(monkeypatch):
    """OpenClaw alone opens the approve doors' execute branch — no native flag."""
    monkeypatch.setattr(settings, "native_executor", False)
    monkeypatch.setattr(settings, "openclaw_executor", True)
    assert executor.enabled() is True
    assert executor.handles({"type": "email.send"}) is True
    monkeypatch.setattr(settings, "openclaw_executor", False)
    assert executor.enabled() is False


# ── dispatch + ledger provenance ──

def test_openclaw_dispatch_and_provenance(monkeypatch):
    monkeypatch.setattr(settings, "openclaw_executor", True)
    monkeypatch.setattr(settings, "native_executor", True)
    statuses: list[tuple] = []
    receipts: list = []

    def fake_status(action_id, status, detail="", *, org_id="", receipt=None):
        statuses.append((action_id, status, detail, org_id))
        receipts.append(receipt)
        return True

    monkeypatch.setattr(executor.ledger, "set_action_status", fake_status)
    # The native vendor clients must never be touched on the OpenClaw route.
    def _boom(*a, **k):
        raise AssertionError("native vendor client used on openclaw route")

    monkeypatch.setattr(executor.google_client, "create_calendar_event", _boom)
    monkeypatch.setattr(executor.google_client, "send_gmail", _boom)
    monkeypatch.setattr(executor.asana_client, "create_task", _boom)

    agent_reply = json.dumps(
        {"ok": True, "receipt": "https://cal/ev-oc", "detail": "event created"}
    )
    calls = _mock_cli(monkeypatch, agent_reply)

    r = executor.execute_approved(
        "org-a", "act-1", {"type": "calendar.create_event", "event": {"title": "x"}}
    )
    assert r["ok"] is True
    assert statuses[-1][:2] == ("act-1", "done") and statuses[-1][3] == "org-a"
    assert "openclaw" in statuses[-1][2] and "https://cal/ev-oc" in statuses[-1][2]
    assert receipts[-1] == {
        "kind": "calendar event", "ref": "https://cal/ev-oc", "route": "openclaw",
    }
    # The CLI call is scoped to the configured agent and a per-action session.
    cmd = calls[-1]
    assert cmd[:2] == [settings.openclaw_bin, "agent"]
    assert f"agent:{settings.openclaw_agent_id}:laura-action-act-1" in cmd

    # Agent-reported failure lands as a "failed" receipt with the reason.
    _mock_cli(monkeypatch, json.dumps(
        {"ok": False, "receipt": "", "detail": "no calendar tool connected"}
    ))
    r2 = executor.execute_approved(
        "org-a", "act-2", {"type": "calendar.create_event", "event": {}}
    )
    assert r2["ok"] is False
    assert statuses[-1][:2] == ("act-2", "failed")
    assert "openclaw" in statuses[-1][2] and "no calendar tool" in statuses[-1][2]

    # An unhandled action type is still skipped with NO ledger write.
    before = len(statuses)
    skipped = executor.execute_approved("org-a", "act-3", {"type": "slack.post"})
    assert skipped.get("skipped") and len(statuses) == before


def test_openclaw_closes_real_ledger_row(monkeypatch, tmp_path):
    """End-to-end against the REAL ledger: the OpenClaw route closes the row
    through the same provenance channel as the native route."""
    _fresh_store(monkeypatch, tmp_path)
    from app import ledger

    ledger._init_db()
    monkeypatch.setattr(settings, "openclaw_executor", True)
    monkeypatch.setattr(settings, "native_executor", True)
    _mock_cli(monkeypatch, json.dumps(
        {"ok": True, "receipt": "m-oc-1", "detail": "sent"}
    ))
    org = settings.demo_org_id
    aid = ledger.new_action_id()
    ledger.record_meeting(
        "https://meet.google.com/abc-defg-hij", "laura", "bot-1",
        {"summary": "s", "actions": [{"item": "email the recap", "action_id": aid}]},
        org_id=org,
    )
    r = executor.execute_approved(
        org, aid, {"type": "email.send", "message": {"to": "x@y.com", "subject": "s"}}
    )
    assert r["ok"] is True
    with store._connect() as c:
        row = c.execute(
            "SELECT status FROM ledger_items WHERE action_id=? AND org_id=?",
            (aid, org),
        ).fetchone()
    assert row is not None and row["status"] == "done"


# ── transport soft-failure modes (run() never raises) ──

def test_run_soft_failures(monkeypatch):
    monkeypatch.setattr(settings, "openclaw_executor", True)
    action = {"type": "email.send", "message": {}}

    # Missing binary / timeout → unreachable, not an exception.
    def raise_fnf(*a, **k):
        raise FileNotFoundError("openclaw")

    monkeypatch.setattr(openclaw_executor.subprocess, "run", raise_fnf)
    r = openclaw_executor.run("org-a", "a1", action)
    assert r["ok"] is False and "unreachable" in r["error"]

    # Non-zero exit / no JSON on stdout.
    monkeypatch.setattr(
        openclaw_executor.subprocess, "run", lambda *a, **k: _Proc("boom", rc=1)
    )
    r = openclaw_executor.run("org-a", "a1", action)
    assert r["ok"] is False and "CLI failed" in r["error"]

    # Garbage stdout.
    monkeypatch.setattr(
        openclaw_executor.subprocess, "run", lambda *a, **k: _Proc("{not json")
    )
    r = openclaw_executor.run("org-a", "a1", action)
    assert r["ok"] is False and "unparseable" in r["error"]

    # Gateway-level run error.
    _mock_cli(monkeypatch, "ignored", status="error")
    r = openclaw_executor.run("org-a", "a1", action)
    assert r["ok"] is False and "agent run error" in r["error"]

    # Agent replied, but not with the mandated JSON receipt.
    _mock_cli(monkeypatch, "sure, I did the thing!")
    r = openclaw_executor.run("org-a", "a1", action)
    assert r["ok"] is False and "receipt" in r["error"]

    # Receipt wrapped in prose/code fences still parses.
    _mock_cli(
        monkeypatch,
        'Done!\n```json\n{"ok": true, "receipt": "t-9", "detail": "created"}\n```',
    )
    r = openclaw_executor.run("org-a", "a1", action)
    assert r["ok"] is True and r["receipt"] == "t-9"
