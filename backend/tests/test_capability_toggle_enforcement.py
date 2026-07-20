"""Per-avatar Slack capability enforcement for Laura's native runtime.

Slack is now a Laura-owned adapter. The owner toggle controls whether an avatar
may propose/execute ``slack.post_message``; no external Cedric MCP tools are
injected into the meeting path.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatar_resolver, store, tool_registry  # noqa: E402
from app.config import settings  # noqa: E402


class _Avatar:
    id = "petra"
    drive_folder_id = ""

    def uses_native_tool(self, name):
        return name == "asana"


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.test/x")
    monkeypatch.setattr(avatar_resolver, "family_allowed", lambda *args: True)
    return store


def _assemble(org="org-1"):
    return tool_registry.assemble(org, _Avatar())


def _native_types(reg: dict) -> set[str]:
    return {
        str(tool.get("type") or "")
        for tool in reg.get("native") or []
        if tool.get("type")
    }


def test_slack_toggle_off_blocks_native_slack_adapter(fresh_store):
    fresh_store.set_avatar_capability("petra", "slack", False)
    reg = _assemble()

    assert reg is not None
    assert "slack.post_message" not in _native_types(reg)
    assert "slack" in reg["disabled_families"]
    assert reg["cedric_mcp"] == []


def test_untouched_avatar_gets_connected_native_slack_adapter(fresh_store):
    reg = _assemble()

    assert reg is not None
    assert "slack.post_message" in _native_types(reg)
    slack = next(
        tool
        for tool in reg["native"]
        if tool.get("type") == "slack.post_message"
    )
    assert slack["connected"] is True
    assert slack["kind"] == "native"
    assert reg["cedric_mcp"] == []


def test_brief_reports_owner_disabled_slack(fresh_store):
    fresh_store.set_avatar_capability("petra", "slack", False)
    text = tool_registry.brief(_assemble())

    assert "disabled for this avatar" in text.lower()
    assert "slack" in text.lower()
    assert "cedric" not in text.lower()


def test_search_reports_owner_disabled_slack(fresh_store):
    fresh_store.set_avatar_capability("petra", "slack", False)
    answer = tool_registry.search(_assemble(), "slack")

    assert "disabled for this avatar" in answer.lower()


def test_specs_for_never_offers_external_cedric_tools(fresh_store):
    from app import tools

    reg = _assemble()

    class _Session:
        tool_registry = reg

    specs = tools.specs_for(_Session(), live=True)
    names = [
        str(spec.get("function", {}).get("name") or spec.get("name") or "")
        for spec in specs
    ]
    assert not any(name.startswith("cedric__") for name in names)
