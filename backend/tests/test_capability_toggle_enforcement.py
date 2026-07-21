"""Per-avatar Slack toggle is ENFORCED on the live tool path and REPORTED in
the avatar's own tool brief.

Owner ask: an avatar whose Slack toggle is off must not be able to use the
Slack-agent (Cedric) tools, and when asked it should say it can't because the
toggle is off; never pretend or silently fail. The gate lives at snapshot
time (tool_registry.assemble, off the hot path); the brief/search text is what
makes the avatar answer honestly."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import store, tool_registry
from app.config import settings


class _Avatar:
    id = "petra"
    drive_folder_id = ""

    def uses_native_tool(self, name):  # petra declares asana; irrelevant here
        return name == "asana"


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)  # re-init schema on the fresh path
    return store


def _assemble(monkeypatch, org="org-1"):
    return tool_registry.assemble(org, _Avatar())


def _enable_mcp(monkeypatch, tools):
    from app import cedric_mcp

    monkeypatch.setattr(cedric_mcp, "enabled", lambda: True)
    monkeypatch.setattr(cedric_mcp, "list_tools", lambda org: tools)


_MCP_TOOLS = [{"name": "gmail_search", "annotations": {"readOnlyHint": True},
               "latencyClass": "fast"}]


def test_slack_toggle_off_blocks_mcp_tools(fresh_store, monkeypatch):
    _enable_mcp(monkeypatch, _MCP_TOOLS)
    fresh_store.set_avatar_capability("petra", "slack", False)
    reg = _assemble(monkeypatch)
    assert reg is not None
    assert reg["slack_blocked"] is True
    assert reg["cedric_mcp"] == []  # no callable Slack-agent tools offered


def test_untouched_avatar_keeps_mcp_tools(fresh_store, monkeypatch):
    _enable_mcp(monkeypatch, _MCP_TOOLS)
    reg = _assemble(monkeypatch)
    assert reg is not None
    assert reg["slack_blocked"] is False
    assert reg["cedric_mcp"] == _MCP_TOOLS  # default behaviour unchanged


def test_brief_tells_the_avatar_to_say_so(fresh_store, monkeypatch):
    _enable_mcp(monkeypatch, _MCP_TOOLS)
    fresh_store.set_avatar_capability("petra", "slack", False)
    reg = _assemble(monkeypatch)
    text = tool_registry.brief(reg)
    assert "toggled it off" in text or "toggled off" in text.lower()
    assert "say you can't" in text.lower()


def test_search_answers_honestly_when_blocked(fresh_store, monkeypatch):
    _enable_mcp(monkeypatch, _MCP_TOOLS)
    fresh_store.set_avatar_capability("petra", "slack", False)
    reg = _assemble(monkeypatch)
    ans = tool_registry.search(reg, "slack")
    assert "toggled off" in ans.lower() or "toggled OFF" in ans
    assert "can't use" in ans.lower()


def test_specs_for_offers_no_cedric_tools_when_blocked(fresh_store, monkeypatch):
    """End of the chain: the model's function specs for a blocked session are
    exactly the native TOOL_SPECS — no cedric__* entries to call."""
    from app import tools

    _enable_mcp(monkeypatch, _MCP_TOOLS)
    fresh_store.set_avatar_capability("petra", "slack", False)
    reg = _assemble(monkeypatch)

    class _S:  # minimal session
        tool_registry = reg

    specs = tools.specs_for(_S(), live=True)
    assert not any(str(s.get("name", "")).startswith("cedric__") for s in specs)
