"""Tool context on join — the org-scoped registry + the two brain tools.

Key-free like the rest of the suite: Cedric's catalog is monkeypatched, the
Google state comes from the sqlite org_oauth table, no vendors touched.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store, tool_registry, tools
from app.cedric import callback as cedric_callback


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    yield store
    importlib.reload(store)


class _Avatar:
    drive_folder_id = ""


_CATALOG = {
    "team_id": "T1",
    "connectors": [
        {"key": "notion", "name": "Notion", "connected": True},
        {"key": "github", "name": "GitHub", "connected": False},
        {"key": "slack", "name": "Slack", "connected": True,
         "needs_reconnect": True, "connect_url": None},  # the #197 null shape
    ],
}


def _connect_brain(st, org_id="org-x"):
    st.set_connection(org_id, "cedric", "cedric-brain", "connected", {"team_id": "T1"})


# ── assemble ───────────────────────────────────────────────────────────

def test_assemble_collects_native_cedric_and_knowledge(fresh_store, monkeypatch):
    st = fresh_store
    _connect_brain(st)
    monkeypatch.setattr(
        cedric_callback, "fetch_org_connectors", lambda org, team="": dict(_CATALOG)
    )
    reg = tool_registry.assemble("org-x", _Avatar())
    assert reg is not None
    names = [t["name"] for t in reg["native"]]
    assert "queue_action" in names and "google_calendar" in names
    # no Google OAuth row → native Google shows not connected
    assert not next(t for t in reg["native"] if t["name"] == "google_calendar")["connected"]
    assert [t["name"] for t in reg["cedric"]["connected"]] == ["Notion", "Slack"]
    assert reg["cedric"]["available"] == ["GitHub"]
    assert reg["knowledge"]["drive_folder"] is False


def test_assemble_without_brain_connection_skips_cedric_fetch(fresh_store, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        cedric_callback, "fetch_org_connectors",
        lambda org, team="": calls.append(org) or dict(_CATALOG),
    )
    reg = tool_registry.assemble("org-x", _Avatar())
    assert reg is not None
    assert calls == []  # no connected Slack agent → never call upstream
    assert reg["cedric"]["connected"] == []


def test_assemble_survives_upstream_failure(fresh_store, monkeypatch):
    st = fresh_store
    _connect_brain(st)
    monkeypatch.setattr(
        cedric_callback, "fetch_org_connectors", lambda org, team="": None
    )
    reg = tool_registry.assemble("org-x", _Avatar())
    assert reg is not None and reg["cedric"]["connected"] == []


def test_assemble_not_linked_sentinel(fresh_store, monkeypatch):
    st = fresh_store
    _connect_brain(st)
    monkeypatch.setattr(
        cedric_callback, "fetch_org_connectors",
        lambda org, team="": {"not_linked": True},
    )
    reg = tool_registry.assemble("org-x", _Avatar())
    assert reg["cedric"]["not_linked"] is True


def test_enabled_notion_survives_unavailable_paid_action_catalog(
    fresh_store, monkeypatch
):
    """Notion's deterministic proxy action does not require Connect's catalog."""
    from app import pipedream_client, pipedream_executor

    class _Cedric:
        id = "cedric"
        drive_folder_id = ""

        @staticmethod
        def uses_native_tool(_name):
            return False

    fresh_store.set_avatar_capability(
        "cedric", "notion", True, org_id="org-x"
    )
    monkeypatch.setattr(pipedream_executor, "enabled", lambda: True)
    monkeypatch.setattr(
        pipedream_executor,
        "app_connected",
        lambda _org, app: app == "notion",
    )
    monkeypatch.setattr(
        pipedream_client,
        "list_accounts",
        lambda _org: [{"id": "acct-synthetic", "app": "notion"}],
    )

    def unavailable_catalog(*_args, **_kwargs):
        raise RuntimeError("catalog plan unavailable")

    monkeypatch.setattr(pipedream_client, "list_actions", unavailable_catalog)
    reg = tool_registry.assemble("org-x", _Cedric())
    assert reg["pd_apps"] == [
        {"slug": "notion", "actions": ["create page"]}
    ]
    assert "Notion (connected via Pipedream" in tool_registry.brief(reg)


# ── brief ──────────────────────────────────────────────────────────────

def test_brief_is_capped_and_honest(fresh_store, monkeypatch):
    st = fresh_store
    _connect_brain(st)
    monkeypatch.setattr(
        cedric_callback, "fetch_org_connectors", lambda org, team="": dict(_CATALOG)
    )
    reg = tool_registry.assemble("org-x", _Avatar())
    text = tool_registry.brief(reg)
    assert len(text) <= tool_registry.MAX_BRIEF_CHARS
    assert "Notion" in text                      # connected → offered
    assert "GitHub" in text and "NOT connected" in text  # never promise
    assert "queued for approval" in text         # the honesty contract
    assert tool_registry.brief(None) == ""


# ── the two brain tools ────────────────────────────────────────────────

def test_dispatch_list_and_search(fresh_store, monkeypatch):
    st = fresh_store
    _connect_brain(st)
    monkeypatch.setattr(
        cedric_callback, "fetch_org_connectors", lambda org, team="": dict(_CATALOG)
    )
    session = st.create("bot1", "https://meet.google.com/x", "laura", org_id="org-x")
    session.tool_registry = tool_registry.assemble("org-x", _Avatar())
    run = tools.dispatch_for(session)

    listed = run("list_capabilities", {})
    assert "Notion" in listed and "queued for approval" in listed

    assert "connected" in run("search_tools", {"query": "notion"})
    assert "NOT connected" in run("search_tools", {"query": "github"})
    assert "no tool matches" in run("search_tools", {"query": "salesforce"})

    # a session without a registry degrades gracefully (never crashes)
    bare = tools.dispatch_for(None)
    assert "unavailable" in bare("list_capabilities", {})


def test_tool_specs_expose_the_new_tools():
    names = [t["function"]["name"] for t in tools.TOOL_SPECS]
    assert "list_capabilities" in names and "search_tools" in names


# ── Asana is declaration-gated (avatar-gated native tool) ───────────────────

def test_asana_tool_is_petra_only(fresh_store, monkeypatch):
    """Asana appears in the tool brief ONLY for avatars built for it (Petra
    and — since her PM specialization — Laura declare native_tools:[asana]);
    every other avatar's brief omits it even when the org has connected Asana.
    Google Calendar stays baseline for all."""
    from app import asana_client, avatars

    st = fresh_store
    monkeypatch.setattr(asana_client, "connected", lambda org_id: True)
    monkeypatch.setattr(
        cedric_callback, "fetch_org_connectors", lambda org, team="": {"connectors": []}
    )

    petra = tool_registry.assemble("org-x", avatars.load("petra"))
    laura = tool_registry.assemble("org-x", avatars.load("laura"))
    cedric = tool_registry.assemble("org-x", avatars.load("cedric"))
    petra_names = [t["name"] for t in petra["native"]]
    laura_names = [t["name"] for t in laura["native"]]
    cedric_names = [t["name"] for t in cedric["native"]]

    assert "asana_tasks" in petra_names          # Petra owns Asana
    assert "asana_tasks" in laura_names          # Laura the PM owns it too
    assert "asana_tasks" not in cedric_names     # the PA never sees it
    # Google Calendar + Gmail are baseline for ALL
    assert "google_calendar" in petra_names and "google_calendar" in laura_names
    assert "gmail_send" in petra_names and "gmail_send" in cedric_names


def test_asana_off_for_petra_when_org_not_connected(fresh_store, monkeypatch):
    from app import asana_client, avatars

    monkeypatch.setattr(asana_client, "connected", lambda org_id: False)
    monkeypatch.setattr(
        cedric_callback, "fetch_org_connectors", lambda org, team="": {"connectors": []}
    )
    petra = tool_registry.assemble("org-x", avatars.load("petra"))
    assert "asana_tasks" not in [t["name"] for t in petra["native"]]


def test_asana_toggle_override_still_works(fresh_store, monkeypatch):
    """An explicit per-avatar toggle overrides the declaration default both
    ways: force Asana OFF for Petra, or ON for Laura."""
    from app import asana_client, avatars

    st = fresh_store
    monkeypatch.setattr(asana_client, "connected", lambda org_id: True)
    monkeypatch.setattr(
        cedric_callback, "fetch_org_connectors", lambda org, team="": {"connectors": []}
    )
    st.set_avatar_capability("petra", "asana", False)  # owner turns Petra's off
    st.set_avatar_capability("laura", "asana", True)   # owner turns Laura's on

    petra = tool_registry.assemble("org-x", avatars.load("petra"))
    laura = tool_registry.assemble("org-x", avatars.load("laura"))
    assert "asana_tasks" not in [t["name"] for t in petra["native"]]
    assert "asana_tasks" in [t["name"] for t in laura["native"]]


def test_capability_verbs_derive_from_the_executor_mapper(monkeypatch):
    """Live capability awareness (owner 2026-07-22): the avatar's claimed
    verbs come from pipedream_executor._MAPPER itself — add an action there
    and the meeting brief knows it with ZERO other changes. The avatar's
    self-knowledge can never lag the execution plane again."""
    from app import pipedream_executor as pe
    from app.brain import tool_registry as tr

    fake_mapper = dict(pe._MAPPER)
    fake_mapper["gmail.snooze"] = ("gmail", lambda *a: None, lambda *a: ("x", ""))
    monkeypatch.setattr(pe, "_MAPPER", fake_mapper)
    monkeypatch.setattr(pe, "enabled", lambda: True)
    monkeypatch.setattr(pe, "app_connected", lambda org, app: True)

    import app.store as store_mod
    monkeypatch.setattr(store_mod, "get_org_oauth", lambda *a, **k: {"tok": "x"},
                        raising=False)
    monkeypatch.setattr(store_mod, "connections_for_org", lambda org: [])
    monkeypatch.setattr(store_mod, "capability_enabled",
                        lambda *a, **k: True, raising=False)

    class _Av:
        id = "petra"
        def uses_native_tool(self, n): return n == "asana"

    reg = tr.assemble("org-test", _Av())
    assert reg is not None
    gm = next(t for t in reg["native"] if t["name"] == "gmail_send")
    assert "snooze" in gm["verbs"]          # the new mapper entry propagated
    assert "snooze" in tr.brief(reg)        # …all the way into the prompt
