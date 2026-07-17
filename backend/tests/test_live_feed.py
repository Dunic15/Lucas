"""The live feed: mid-meeting the avatar speaks from CURRENT state, not the
join-time snapshot.

Three seams, all key-free:
  1. LIVE Asana read tools — offered per-session (specs_for gates on the
     session.asana_live flag set at session start), org threaded from the
     session, reads only (writes stay behind queue_action → approval).
  2. PERIODIC context re-pull — transcript webhooks re-pull context_url once
     the last pull is older than CONTEXT_REFRESH_SECONDS (0 = legacy
     one-shot; a quiet meeting never re-pulls).
  3. Context PUSH door — POST /sessions/{bot_id}/context replaces the brief
     in real time; per-org bearers only reach their own org's sessions.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import asana_client, cedric, ledger, store, tools
from app.cedric import callback as cedric_callback
from app.config import settings


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return store


CONTEXT_URL = "https://cedric.example/api/laura/context"


def _orchestrated(store_mod, bot_id: str, org_id: str = ""):
    s = store_mod.create(
        bot_id=bot_id,
        meeting_url=f"https://meet.google.com/{bot_id}",
        avatar_id="cedric",
        org_id=org_id or store_mod.DEMO_ORG_ID,
    )
    s.integration = {
        "callback_url": "",
        "context_url": CONTEXT_URL,
        "external_ref": {},
        "brief": "stale booking-time brief",
        "meeting": {},
        "mission": "",
        "org_id": org_id,
        "context_refreshed": False,
    }
    return s


def _transcript_payload(bot_id: str, text: str = "hello there everyone"):
    return {
        "event": "transcript.data",
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "words": [{"text": t} for t in text.split()],
                "participant": {"id": 7, "name": "Ben"},
            },
        },
    }


def _post_webhook(*payloads: dict) -> None:
    async def _run() -> None:
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            for payload in payloads:
                await ac.post("/webhooks/recall", json=payload)
                pending = asyncio.all_tasks() - {asyncio.current_task()}
                if pending:
                    await asyncio.wait(pending, timeout=2.0)

    asyncio.run(_run())


@pytest.fixture
def fetch_calls(monkeypatch):
    calls: list[dict] = []

    def fake_fetch(integration):
        calls.append(dict(integration or {}))
        return {"meeting": {"title": "Q3 sync"}, "brief_markdown": "FRESH BRIEF"}

    monkeypatch.setattr(cedric_callback, "fetch_context", fake_fetch)
    return calls


# ───────────────────── 1. live Asana read tools ─────────────────────

_ASANA_TOOL_NAMES = {"asana_projects", "asana_tasks", "asana_search"}


def _spec_names(specs):
    return {s["function"]["name"] for s in specs}


def test_asana_specs_gated_on_session_flag():
    assert not (_ASANA_TOOL_NAMES & _spec_names(tools.specs_for(None)))
    off = SimpleNamespace(asana_live=False)
    assert not (_ASANA_TOOL_NAMES & _spec_names(tools.specs_for(off)))
    on = SimpleNamespace(asana_live=True)
    assert _ASANA_TOOL_NAMES <= _spec_names(tools.specs_for(on))


def test_asana_tools_read_live_with_session_org(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        asana_client, "list_projects",
        lambda org, **kw: seen.append(("projects", org))
        or {"ok": True, "projects": [{"gid": "p1", "name": "Launch"}]},
    )
    monkeypatch.setattr(
        asana_client, "project_tasks",
        lambda org, project, **kw: seen.append(("tasks", org, project))
        or {"ok": True, "tasks": [{"name": "Ship it", "completed": False,
                                   "assignee": "Dana", "due_on": ""}]},
    )
    monkeypatch.setattr(
        asana_client, "find_tasks",
        lambda org, query, **kw: seen.append(("find", org, query))
        or {"ok": True, "tasks": []},
    )
    session = SimpleNamespace(org_id="org_live", asana_live=True)

    out = json.loads(tools.dispatch("asana_projects", {}, session=session))
    assert out["projects"][0]["name"] == "Launch"
    out = json.loads(
        tools.dispatch("asana_tasks", {"project": "Launch"}, session=session)
    )
    assert out["tasks"][0]["name"] == "Ship it"
    tools.dispatch("asana_search", {"query": "ship"}, session=session)
    assert seen == [
        ("projects", "org_live"),
        ("tasks", "org_live", "Launch"),
        ("find", "org_live", "ship"),
    ]


def test_asana_tools_honest_errors(monkeypatch):
    # No live session (direct web avatar) → say so, never guess.
    assert tools.dispatch("asana_projects", {}, session=None).startswith("error:")
    # Asana down → the client's error is relayed as a spoken-safe string.
    monkeypatch.setattr(
        asana_client, "list_projects",
        lambda org, **kw: {"ok": False, "error": "asana returned 500"},
    )
    session = SimpleNamespace(org_id="org_x", asana_live=True)
    out = tools.dispatch("asana_projects", {}, session=session)
    assert out.startswith("error:") and "500" in out
    # Missing args → actionable error, not a crash.
    assert tools.dispatch("asana_tasks", {}, session=session).startswith("error:")
    assert tools.dispatch("asana_search", {}, session=session).startswith("error:")


# ──────────────── 2. periodic context re-pull (live feed) ────────────────


def test_transcript_repulls_after_window(fresh_store, fetch_calls, monkeypatch):
    monkeypatch.setattr(settings, "context_refresh_seconds", 120.0)
    s = _orchestrated(fresh_store, "bot_lf1")
    _post_webhook(_transcript_payload("bot_lf1"))
    assert len(fetch_calls) == 1
    assert s.integration["brief"] == "FRESH BRIEF"

    # Same window: more talk, no re-pull.
    _post_webhook(_transcript_payload("bot_lf1", "more talk"))
    assert len(fetch_calls) == 1

    # Age the last pull past the window → the next transcript re-pulls.
    s.integration = {**s.integration, "context_refreshed_at": time.time() - 999,
                     "brief": "now stale again"}
    _post_webhook(_transcript_payload("bot_lf1", "and more talk"))
    assert len(fetch_calls) == 2
    assert s.integration["brief"] == "FRESH BRIEF"


def test_zero_interval_restores_one_shot(fresh_store, fetch_calls, monkeypatch):
    monkeypatch.setattr(settings, "context_refresh_seconds", 0.0)
    s = _orchestrated(fresh_store, "bot_lf2")
    _post_webhook(_transcript_payload("bot_lf2"))
    assert len(fetch_calls) == 1
    s.integration = {**s.integration, "context_refreshed_at": time.time() - 999}
    _post_webhook(_transcript_payload("bot_lf2", "later"))
    assert len(fetch_calls) == 1  # one-shot: never again


def test_legacy_flag_without_timestamp_does_not_repull_immediately(
    fresh_store, fetch_calls, monkeypatch
):
    # A session refreshed by the PRE-upgrade code (flag, no timestamp) mid-
    # deploy: the flag counts as "just pulled" — no thundering re-pull.
    monkeypatch.setattr(settings, "context_refresh_seconds", 120.0)
    s = _orchestrated(fresh_store, "bot_lf3")
    s.integration = {**s.integration, "context_refreshed": True}
    _post_webhook(_transcript_payload("bot_lf3"))
    assert len(fetch_calls) == 0


# ──────────────────── 3. the context PUSH door ────────────────────


@pytest.fixture
def client(fresh_store):
    return TestClient(main_module.app)


PUSH = {"context": {"meeting": {"title": "Q3 sync (running)"},
                    "brief_markdown": "PUSHED: API-12 just closed.",
                    "mission": "close the renewal"}}


def test_push_updates_brief_meeting_mission_now(client, fresh_store):
    s = _orchestrated(fresh_store, "bot_p1")
    resp = client.post("/sessions/bot_p1/context", json=PUSH)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert s.integration["brief"].startswith("PUSHED")
    assert s.integration["meeting"]["title"] == "Q3 sync (running)"
    assert s.integration["mission"] == "close the renewal"
    # The very next answer speaks from it (inject_brief re-reads per turn) …
    assert "API-12" in cedric.inject_brief(s, "")
    # … and a push resets the pull window (pushing orchestrators aren't polled).
    assert s.integration["context_refreshed_at"] > 0


def test_push_scope_per_org_bearer(client, fresh_store):
    _orchestrated(fresh_store, "bot_p2", org_id="org_a")
    raw_b = store.mint_org_token("org_b", "test")
    resp = client.post(
        "/sessions/bot_p2/context", json=PUSH,
        headers={"Authorization": f"Bearer {raw_b}"},
    )
    assert resp.status_code == 404  # foreign org: existence never leaks
    raw_a = store.mint_org_token("org_a", "test")
    resp = client.post(
        "/sessions/bot_p2/context", json=PUSH,
        headers={"Authorization": f"Bearer {raw_a}"},
    )
    assert resp.status_code == 200


def test_push_validation(client, fresh_store):
    assert client.post("/sessions/nope/context", json=PUSH).status_code == 404
    s = _orchestrated(fresh_store, "bot_p3")
    big = {"context": {"brief_markdown": "x" * (33 * 1024)}}
    assert client.post("/sessions/bot_p3/context", json=big).status_code == 400
    assert client.post(
        "/sessions/bot_p3/context", json={"context": {}}
    ).status_code == 400
    assert s.integration["brief"] == "stale booking-time brief"  # untouched


def test_push_requires_auth_when_token_set(client, fresh_store, monkeypatch):
    _orchestrated(fresh_store, "bot_p4")
    monkeypatch.setattr(settings, "laura_api_token", "sekrit")
    assert client.post("/sessions/bot_p4/context", json=PUSH).status_code == 401
    ok = client.post(
        "/sessions/bot_p4/context", json=PUSH,
        headers={"Authorization": "Bearer sekrit"},
    )
    assert ok.status_code == 200
