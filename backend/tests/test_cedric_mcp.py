"""Contract tests for the Laura→Cedric MCP tool bridge (Handshake v3).

Cedric's server is mocked (no network): we fake ``cedric_mcp.httpx.post`` with a
tiny JSON-RPC server that dispatches on the posted method, and assert Laura's
CLIENT side matches the agreed contract exactly — endpoint, headers
(X-Laura-Org-Id + bearer), tools/list, tools/call result shapes (success /
isError / approval_required / truncated), the error discriminators, live-path
gating (readOnlyHint && latencyClass=fast), and the tools.py dispatch routing.

Key-free and offline; the flag is forced on per test.

Handshake operations covered: mcp-initialize, tools-list, tools-call.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import cedric_mcp  # noqa: E402
from app.cedric import callback as cedric_callback  # noqa: E402
from app.config import settings  # noqa: E402

ORG = "org_test"
ENDPOINT = "https://cedric.example/api/laura/mcp"


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self._p = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._p


def _server(monkeypatch, *, tools_list=None, call=None, sid="sess-1",
            initialize_error=None, capture=None):
    """Install a fake Cedric MCP server. ``call`` is the tools/call result (or a
    callable(args)->result); ``tools_list`` the tools/list tools array. Records
    every request into ``capture`` (a list) when provided."""
    def fake_post(url, headers=None, json=None, timeout=None):
        if capture is not None:
            capture.append({"url": url, "headers": headers or {}, "body": json})
        method = (json or {}).get("method")
        if method == "initialize":
            if initialize_error is not None:
                return _Resp({"jsonrpc": "2.0", "id": json.get("id"), "error": initialize_error})
            return _Resp({"jsonrpc": "2.0", "id": json.get("id"),
                          "result": {"protocolVersion": "2025-06-18",
                                     "serverInfo": {"name": "cedric"}}},
                         headers={"Mcp-Session-Id": sid} if sid else {})
        if method == "notifications/initialized":
            return _Resp({}, status=202)
        if method == "tools/list":
            return _Resp({"jsonrpc": "2.0", "id": json.get("id"),
                          "result": {"tools": tools_list or []}})
        if method == "tools/call":
            res = call(json["params"]) if callable(call) else call
            if isinstance(res, dict) and "error" in res and set(res) == {"error"}:
                return _Resp({"jsonrpc": "2.0", "id": json.get("id"), "error": res["error"]})
            if isinstance(res, _Resp):
                return res
            return _Resp({"jsonrpc": "2.0", "id": json.get("id"), "result": res})
        return _Resp({"jsonrpc": "2.0", "id": (json or {}).get("id"), "result": {}})

    monkeypatch.setattr(cedric_mcp.httpx, "post", fake_post)


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(settings, "cedric_mcp_enabled", True)
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric.example/api/laura/orgs")
    monkeypatch.setattr(settings, "cedric_orgs_token", "deploy-bearer")
    # Deterministic bearer — isolate from the secret registry / SSM.
    monkeypatch.setattr(cedric_callback, "_bearer_for", lambda org, legacy="": "deploy-bearer")
    cedric_mcp._reset()
    yield
    cedric_mcp._reset()


# ── flag gating ──

def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "cedric_mcp_enabled", False)
    assert cedric_mcp.enabled() is False
    assert cedric_mcp.list_tools(ORG) is None
    r = cedric_mcp.call_tool(ORG, "gmail_search", {})
    assert r["error_kind"] == "disabled"


# ── endpoint + headers (contract fidelity) ──

def test_endpoint_and_headers(monkeypatch):
    cap: list = []
    _server(monkeypatch, tools_list=[], capture=cap)
    cedric_mcp.list_tools(ORG)
    assert cap, "no request captured"
    assert cap[0]["url"] == ENDPOINT
    h = cap[0]["headers"]
    assert h["X-Laura-Org-Id"] == ORG          # required on EVERY request
    assert h["Authorization"] == "Bearer deploy-bearer"
    # Later requests carry the session id the server minted on initialize.
    later = [c for c in cap if (c["body"] or {}).get("method") == "tools/list"]
    assert later and later[0]["headers"].get("Mcp-Session-Id") == "sess-1"


# ── tools/list + mapping + live gating ──

_TOOLS = [
    {"name": "gmail_search", "description": "search gmail",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
     "annotations": {"readOnlyHint": True, "latencyClass": "fast"}},
    {"name": "notion_slow_read", "description": "slow read",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "latencyClass": "slow"}},
    {"name": "gmail_send", "description": "send mail",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": False, "latencyClass": "fast"}},
]


def test_list_tools_and_live_gating(monkeypatch):
    _server(monkeypatch, tools_list=_TOOLS)
    got = cedric_mcp.list_tools(ORG)
    assert [t["name"] for t in got] == ["gmail_search", "notion_slow_read", "gmail_send"]

    live = cedric_mcp.to_function_specs(got, live=True)
    # Only read-only AND fast survives on the live path.
    assert [s["function"]["name"] for s in live] == ["cedric__gmail_search"]
    assert live[0]["function"]["parameters"] == _TOOLS[0]["inputSchema"]

    off = cedric_mcp.to_function_specs(got, live=False)
    assert {s["function"]["name"] for s in off} == {
        "cedric__gmail_search", "cedric__notion_slow_read", "cedric__gmail_send"}


def test_is_live_safe_defaults_slow(monkeypatch):
    assert cedric_mcp.is_live_safe({"annotations": {"readOnlyHint": True, "latencyClass": "fast"}})
    # missing latencyClass defaults to slow → not live-safe
    assert not cedric_mcp.is_live_safe({"annotations": {"readOnlyHint": True}})
    assert not cedric_mcp.is_live_safe({"annotations": {"readOnlyHint": False, "latencyClass": "fast"}})


# ── tools/call result shapes ──

def test_call_success(monkeypatch):
    _server(monkeypatch, call={"content": [{"type": "text", "text": "3 results"}]})
    r = cedric_mcp.call_tool(ORG, "gmail_search", {"q": "invoice"})
    assert r["ok"] is True and r["text"] == "3 results" and r["truncated"] is False
    assert cedric_mcp.result_to_model_text(r) == "3 results"


def test_call_truncated(monkeypatch):
    _server(monkeypatch, call={"content": [{"type": "text", "text": "big"}],
                               "structuredContent": {"result_truncated": True}})
    r = cedric_mcp.call_tool(ORG, "gmail_search", {})
    assert r["ok"] and r["truncated"] is True
    assert "[result truncated]" in cedric_mcp.result_to_model_text(r)


def test_call_tool_error(monkeypatch):
    _server(monkeypatch, call={"isError": True, "content": [{"type": "text", "text": "boom"}]})
    r = cedric_mcp.call_tool(ORG, "gmail_search", {})
    assert r["is_error"] is True
    assert "boom" in cedric_mcp.result_to_model_text(r)


def test_call_approval_required_not_executed(monkeypatch):
    _server(monkeypatch, call={"isError": False, "structuredContent": {
        "status": "approval_required", "summary": "send email to Acme",
        "proposed_action": {"tool": "gmail_send", "to": "acme"}}})
    r = cedric_mcp.call_tool(ORG, "gmail_send", {"to": "acme"})
    assert r["approval_required"] is True
    assert r["summary"] == "send email to Acme"
    txt = cedric_mcp.result_to_model_text(r)
    assert "queued" in txt and "not done" in txt


# ── error discriminators ──

@pytest.mark.parametrize("code,data,kind", [
    (-32602, None, "invalid_args"),
    (-32001, {"error": "not_linked"}, "not_linked"),
    (-32001, {"error": "unknown_tool"}, "unknown_tool"),
    (-32001, {"error": "tool_not_connected"}, "tool_not_connected"),
    (-32001, {"error": "scope_missing"}, "scope_missing"),
])
def test_call_error_discriminators(monkeypatch, code, data, kind):
    err = {"code": code, "message": "nope"}
    if data is not None:
        err["data"] = data
    _server(monkeypatch, call={"error": err})
    r = cedric_mcp.call_tool(ORG, "some_tool", {})
    assert r["error_kind"] == kind
    # every kind has a human-facing rendering (no raw codes leak to the model)
    assert cedric_mcp.result_to_model_text(r)


def test_transport_401_is_auth(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        if (json or {}).get("method") == "initialize":
            return _Resp({}, status=401)
        return _Resp({}, status=401)
    monkeypatch.setattr(cedric_mcp.httpx, "post", fake_post)
    r = cedric_mcp.call_tool(ORG, "gmail_search", {})
    assert r["error_kind"] == "auth"


def test_transport_429_is_rate_limited(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        m = (json or {}).get("method")
        if m == "initialize":
            return _Resp({"jsonrpc": "2.0", "id": 1, "result": {}}, headers={"Mcp-Session-Id": "s"})
        if m == "notifications/initialized":
            return _Resp({}, 202)
        return _Resp({}, status=429)
    monkeypatch.setattr(cedric_mcp.httpx, "post", fake_post)
    r = cedric_mcp.call_tool(ORG, "gmail_search", {})
    assert r["error_kind"] == "rate_limited"


# ── _meta carried on tools/call ──

def test_call_sends_meta(monkeypatch):
    cap: list = []
    _server(monkeypatch, call={"content": [{"type": "text", "text": "ok"}]}, capture=cap)
    cedric_mcp.call_tool(ORG, "gmail_search", {"q": "x"},
                         meta={"actor": "avatar", "source": "meeting", "ref": "bot_1"})
    call = next(c for c in cap if (c["body"] or {}).get("method") == "tools/call")
    params = call["body"]["params"]
    assert params["name"] == "gmail_search"
    assert params["arguments"] == {"q": "x"}
    assert params["_meta"] == {"actor": "avatar", "source": "meeting", "ref": "bot_1"}


# ── tools.py integration: specs_for + dispatch routing ──

def _session(mcp_tools):
    return SimpleNamespace(org_id=ORG, bot_id="bot_1",
                           tool_registry={"cedric_mcp": mcp_tools})


def test_specs_for_live_appends_only_fast_reads(monkeypatch):
    from app import tools
    sess = _session(_TOOLS)
    live = tools.specs_for(sess, live=True)
    names = [s["function"]["name"] for s in live]
    assert "cedric__gmail_search" in names          # read+fast
    assert "cedric__gmail_send" not in names         # write
    assert "cedric__notion_slow_read" not in names   # slow
    # native tools are always present
    assert any(s["function"]["name"] == "queue_action" for s in live)


def test_dispatch_routes_cedric_tool(monkeypatch):
    from app import tools
    _server(monkeypatch, call={"content": [{"type": "text", "text": "found 2"}]})
    sess = _session(_TOOLS)
    out = tools.dispatch("cedric__gmail_search", {"q": "invoice"}, session=sess, live=True)
    assert out == "found 2"


def test_dispatch_approval_captures_via_queue_action(monkeypatch):
    from app import tools
    _server(monkeypatch, call={"isError": False, "structuredContent": {
        "status": "approval_required", "summary": "send mail to Acme",
        "proposed_action": {}}})
    captured: list = []
    monkeypatch.setattr(tools, "queue_action",
                        lambda action="", session=None, **k: captured.append(action) or "queued")
    sess = _session(_TOOLS)
    out = tools.dispatch("cedric__gmail_send", {}, session=sess, live=False)
    assert "queued" in out and "not done" in out
    assert captured == ["send mail to Acme"]   # landed in the approve queue


def test_dispatch_cedric_without_org_is_soft_error(monkeypatch):
    from app import tools
    sess = SimpleNamespace(org_id="", bot_id="", tool_registry={"cedric_mcp": _TOOLS})
    out = tools.dispatch("cedric__gmail_search", {}, session=sess, live=True)
    assert "can't use that tool" in out or "no org" in out
