"""Meeting-start tool context for Laura's native adapter registry."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatar_resolver, native_runtime, store, tool_registry, tools  # noqa: E402


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    yield store
    importlib.reload(store)


class _Avatar:
    id = "laura"
    drive_folder_id = ""

    @staticmethod
    def uses_native_tool(_name: str) -> bool:
        return False


_CATALOG = [
    {
        "type": "calendar.create_event",
        "family": "google",
        "name": "Google Calendar",
        "connected": True,
        "write": True,
        "approval": "approve",
        "kind": "native",
    },
    {
        "type": "email.send",
        "family": "google",
        "name": "Gmail",
        "connected": True,
        "write": True,
        "approval": "approve",
        "kind": "native",
    },
    {
        "type": "slack.post_message",
        "family": "slack",
        "name": "Slack",
        "connected": False,
        "write": True,
        "approval": "approve",
        "kind": "native",
    },
]


def _allow_all(monkeypatch):
    monkeypatch.setattr(avatar_resolver, "family_allowed", lambda *args: True)


def test_assemble_collects_only_laura_native_tools(fresh_store, monkeypatch):
    _allow_all(monkeypatch)
    monkeypatch.setattr(native_runtime, "catalog", lambda org: list(_CATALOG))

    reg = tool_registry.assemble("org-x", _Avatar())

    assert reg is not None
    names = [tool["name"] for tool in reg["native"]]
    assert "queue_action" in names
    assert "calendar.create_event" in names
    assert "email.send" in names
    assert "slack.post_message" in names
    # External Cedric discovery is intentionally disabled during migration.
    assert reg["cedric"] == {
        "connected": [],
        "available": [],
        "not_linked": False,
    }
    assert reg["cedric_mcp"] == []
    assert reg["knowledge"]["drive_folder"] is False


def test_brief_is_capped_connection_aware_and_honest(fresh_store, monkeypatch):
    _allow_all(monkeypatch)
    monkeypatch.setattr(native_runtime, "catalog", lambda org: list(_CATALOG))

    text = tool_registry.brief(tool_registry.assemble("org-x", _Avatar()))

    assert len(text) <= tool_registry.MAX_BRIEF_CHARS
    assert "Google Calendar" in text and "Gmail" in text
    assert "Slack" in text and "Not connected" in text
    assert "executed by Laura" in text
    assert "queued for approval" in text
    assert "Cedric" not in text
    assert tool_registry.brief(None) == ""


def test_dispatch_list_and_search(fresh_store, monkeypatch):
    _allow_all(monkeypatch)
    monkeypatch.setattr(native_runtime, "catalog", lambda org: list(_CATALOG))
    session = fresh_store.create(
        "bot1", "https://meet.google.com/x", "laura", org_id="org-x"
    )
    session.tool_registry = tool_registry.assemble("org-x", _Avatar())
    run = tools.dispatch_for(session)

    listed = run("list_capabilities", {})
    assert "Google Calendar" in listed and "queued for approval" in listed
    assert "connected" in run("search_tools", {"query": "calendar"})
    assert "NOT connected" in run("search_tools", {"query": "slack"})
    assert "no Laura-native tool matches" in run(
        "search_tools", {"query": "salesforce"}
    )

    bare = tools.dispatch_for(None)
    assert "unavailable" in bare("list_capabilities", {})


def test_tool_specs_expose_capability_tools():
    names = [tool["function"]["name"] for tool in tools.TOOL_SPECS]
    assert "list_capabilities" in names
    assert "search_tools" in names


def test_asana_is_avatar_gated(fresh_store, monkeypatch):
    from app import avatars

    _allow_all(monkeypatch)
    catalog = list(_CATALOG) + [
        {
            "type": "asana.create_task",
            "family": "asana",
            "name": "Asana",
            "connected": True,
            "write": True,
            "approval": "approve",
            "kind": "native",
        }
    ]
    monkeypatch.setattr(native_runtime, "catalog", lambda org: list(catalog))

    petra = tool_registry.assemble("org-x", avatars.load("petra"))
    laura = tool_registry.assemble("org-x", avatars.load("laura"))

    assert "asana.create_task" in [tool["name"] for tool in petra["native"]]
    assert "asana.create_task" not in [tool["name"] for tool in laura["native"]]
    assert "calendar.create_event" in [tool["name"] for tool in petra["native"]]
    assert "calendar.create_event" in [tool["name"] for tool in laura["native"]]


def test_explicit_capability_toggle_overrides_avatar_default(fresh_store, monkeypatch):
    from app import avatars

    _allow_all(monkeypatch)
    catalog = list(_CATALOG) + [
        {
            "type": "asana.create_task",
            "family": "asana",
            "name": "Asana",
            "connected": True,
            "write": True,
            "approval": "approve",
            "kind": "native",
        }
    ]
    monkeypatch.setattr(native_runtime, "catalog", lambda org: list(catalog))

    fresh_store.set_avatar_capability("petra", "asana", False)
    fresh_store.set_avatar_capability("laura", "asana", True)

    petra = tool_registry.assemble("org-x", avatars.load("petra"))
    laura = tool_registry.assemble("org-x", avatars.load("laura"))

    assert "asana.create_task" not in [tool["name"] for tool in petra["native"]]
    assert "asana.create_task" in [tool["name"] for tool in laura["native"]]
