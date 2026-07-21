"""Laura → Cedric programmatic tool bridge; the MCP CLIENT side.

Handshake contract v3 (2026-07-16, session hsk_ses_hbktrhxxsm0gr6ryrnyn): Cedric
exposes its per-workspace connected tools as an MCP server over Streamable HTTP
(JSON-RPC 2.0, plain JSON, no SSE in v1) at ``POST {cedric}/api/laura/mcp``. Laura
is the client: discover tools with ``tools/list`` (at meeting-join, off the hot
path) and invoke one with ``tools/call``.

Faithful to the contract:
  - Auth = the EXISTING Laura→Cedric deployment bearer (cedric.callback._bearer_for)
    + ``X-Laura-Org-Id`` on EVERY request. No new auth surface.
  - Every tool carries ``annotations.readOnlyHint`` (MCP std) + ``latencyClass``
    ('fast'|'slow', default 'slow'). Laura's LIVE meeting path calls ONLY
    readOnlyHint=true AND latencyClass=='fast', client-budgeted (~2s). Off-path
    (dashboard / post-meeting) may use the rest, up to ~90s.
  - Team-scope connectors only (Cedric enforces; Laura never sees personal ones).
  - Approval-gated WRITES do NOT execute in v1: Cedric returns isError=false +
    structuredContent {status:'approval_required', summary, proposed_action}. We
    surface that as a capture, never a "done".
  - 64KB result cap → structuredContent.result_truncated=true when truncated.
  - Errors: JSON-RPC -32602 (bad args) / -32001 with data.error discriminator
    ('not_linked' | 'unknown_tool' | 'tool_not_connected' | 'scope_missing');
    tool runtime failure = MCP isError=true content.
  - _meta: user_ref (opaque Laura user id, NEVER an email), actor, source, ref,
    idempotency_key (reserved). No auto-retry on writes.

PII: arguments carry distilled fields only. NEVER transcript content; results
and logs never carry account ids/tokens (contract) and we log tool names only.

Everything here is gated by ``settings.cedric_mcp_enabled`` (default OFF); when
off, nothing in this module runs and the caller behaves exactly as before.

Handshake operations implemented (client side, direction laura->cedric):
    mcp-initialize | tools-list | tools-call
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from ..config import settings

# MCP protocol version we advertise on initialize. Cedric negotiates per the MCP
# spec; a mismatch is theirs to reconcile in the initialize result.
_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "laura", "version": "1"}

# Prefix for Cedric tool names inside Laura's function-calling registry, so they
# never collide with the native tools (tools._DISPATCH) and the dispatch can
# route them back here by name. The bare Cedric tool name follows the prefix.
TOOL_PREFIX = "cedric__"

# Per-org MCP session cache: org_id -> (session_id, expires_at). session_id is
# the Mcp-Session-Id the server returned on initialize ("" when the server is
# stateless). Lets a live tools/call be a single POST after the first join.
_SESSION_TTL_S = 600.0
_SESSION_LOCK = threading.Lock()
_SESSIONS: dict[str, tuple[str, float]] = {}


def enabled() -> bool:
    """The bridge is usable only when explicitly turned on AND Cedric is wired."""
    return bool(settings.cedric_mcp_enabled) and bool(settings.cedric_orgs_url.strip())


def _reset() -> None:
    """Test seam; clear the process-global session cache."""
    with _SESSION_LOCK:
        _SESSIONS.clear()


def _endpoint() -> str:
    """The MCP URL; sibling of the existing /connectors and /orgs routes.
    CEDRIC_ORGS_URL points at ``.../api/laura/orgs``; swap the last segment."""
    base = settings.cedric_orgs_url.strip()
    return base.rstrip("/").rsplit("/", 1)[0] + "/mcp"


def _headers(org_id: str, session_id: str = "") -> dict[str, str]:
    from . import callback as cedric_callback  # lazy: avoid load cycle

    token = cedric_callback._bearer_for(
        org_id, settings.cedric_orgs_token.strip() or settings.laura_api_token.strip()
    )
    h = {
        "content-type": "application/json",
        "accept": "application/json",
        "X-Laura-Org-Id": org_id,
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


class McpError(Exception):
    """A JSON-RPC / transport failure. ``kind`` is the contract discriminator
    (data.error) when present: not_linked | unknown_tool | tool_not_connected |
    scope_missing | invalid_args | transport | auth | rate_limited."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind
        self.message = message or kind


def _map_rpc_error(code: int, data: Any, message: str) -> McpError:
    disc = ""
    if isinstance(data, dict):
        disc = str(data.get("error") or "").strip()
    if code == -32602:
        return McpError("invalid_args", message)
    if disc in {"not_linked", "unknown_tool", "tool_not_connected", "scope_missing"}:
        return McpError(disc, message)
    return McpError("rpc_error", message or f"rpc {code}")


def _post(org_id: str, payload: dict, *, timeout: float, session_id: str) -> httpx.Response:
    return httpx.post(
        _endpoint(), headers=_headers(org_id, session_id), json=payload, timeout=timeout
    )


def _rpc(org_id: str, method: str, params: dict | None, *, timeout: float,
         session_id: str, _id: int) -> tuple[Any, str]:
    """One JSON-RPC request → (result, new_session_id). Raises McpError.

    Transport 401 -> auth; 429 -> rate_limited (contract); JSON-RPC error ->
    _map_rpc_error. Never logs bodies (may echo args); tool names only."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    try:
        resp = _post(org_id, payload, timeout=timeout, session_id=session_id)
    except httpx.TimeoutException:
        raise McpError("timeout", f"{method} timed out") from None
    except Exception as e:  # noqa: BLE001
        raise McpError("transport", f"{method} failed ({type(e).__name__})") from None
    if resp.status_code == 401:
        raise McpError("auth", "bearer rejected")
    if resp.status_code == 429:
        raise McpError("rate_limited", "rate limited")
    new_sid = resp.headers.get("Mcp-Session-Id", "") or session_id
    if resp.status_code >= 300:
        raise McpError("transport", f"{method} HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        raise McpError("transport", f"{method} non-JSON response") from None
    if isinstance(body, dict) and body.get("error"):
        err = body["error"] or {}
        raise _map_rpc_error(
            int(err.get("code") or 0), err.get("data"), str(err.get("message") or "")
        )
    result = body.get("result") if isinstance(body, dict) else None
    return result, new_sid


def _notify(org_id: str, method: str, *, timeout: float, session_id: str) -> None:
    """A JSON-RPC notification (no id, no response body expected)."""
    try:
        _post(org_id, {"jsonrpc": "2.0", "method": method}, timeout=timeout,
              session_id=session_id)
    except Exception:  # noqa: BLE001; a lost 'initialized' notice is non-fatal
        pass


def _ensure_session(org_id: str, *, timeout: float, force: bool = False) -> str:
    """Return a live MCP session id for the org, doing the initialize handshake
    (initialize -> notifications/initialized) at most once per TTL. '' is a valid
    session id for a stateless server; it is still cached so we don't re-init."""
    now = time.time()
    if not force:
        with _SESSION_LOCK:
            cached = _SESSIONS.get(org_id)
            if cached and cached[1] > now:
                return cached[0]
    params = {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": _CLIENT_INFO,
    }
    _result, sid = _rpc(org_id, "initialize", params, timeout=timeout,
                        session_id="", _id=1)
    _notify(org_id, "notifications/initialized", timeout=timeout, session_id=sid)
    with _SESSION_LOCK:
        _SESSIONS[org_id] = (sid, now + _SESSION_TTL_S)
    return sid


def _invalidate(org_id: str) -> None:
    with _SESSION_LOCK:
        _SESSIONS.pop(org_id, None)


def list_tools(org_id: str) -> list[dict] | None:
    """Discover the org's callable tools (MCP tools/list). Call at meeting-join,
    OFF the hot path. Returns the raw McpTool list (name, description,
    inputSchema, annotations), or None when disabled / unlinked / any failure -
    the caller then simply has no Cedric tools this session (never blocks join)."""
    if not enabled() or not (org_id or "").strip():
        return None
    t = settings.cedric_mcp_offpath_timeout_s
    try:
        sid = _ensure_session(org_id, timeout=t)
        result, _sid = _rpc(org_id, "tools/list", {}, timeout=t, session_id=sid, _id=2)
    except McpError as e:
        if e.kind not in {"transport", "auth", "timeout"}:
            print(f"[cedric-mcp] tools/list unavailable ({e.kind})", flush=True)
            return None
        # A stale cached session can 4xx; drop it and retry once fresh.
        try:
            sid = _ensure_session(org_id, timeout=t, force=True)
            result, _sid = _rpc(org_id, "tools/list", {}, timeout=t, session_id=sid, _id=2)
        except McpError as e2:
            print(f"[cedric-mcp] tools/list failed ({e2.kind})", flush=True)
            return None
    tools = (result or {}).get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return None
    out = [t for t in tools if isinstance(t, dict) and t.get("name")]
    print(f"[cedric-mcp] tools/list ok count={len(out)}", flush=True)
    return out


def is_live_safe(tool: dict) -> bool:
    """A tool is safe on the LIVE meeting path iff it is read-only AND fast -
    exactly the contract's gate. Missing latencyClass defaults to 'slow'."""
    ann = tool.get("annotations") or {}
    return bool(ann.get("readOnlyHint")) and str(ann.get("latencyClass") or "slow") == "fast"


def to_function_specs(tools: list[dict], *, live: bool) -> list[dict]:
    """Map McpTool[] → OpenAI function-calling specs for complete_with_tools.
    ``live=True`` keeps only live-safe (read+fast) tools. Names are prefixed so
    the dispatch can route them and they never collide with native tools."""
    specs: list[dict] = []
    for t in tools or []:
        if live and not is_live_safe(t):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        schema = t.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        specs.append({
            "type": "function",
            "function": {
                "name": TOOL_PREFIX + name,
                "description": str(t.get("description") or name),
                "parameters": schema,
            },
        })
    return specs


def _content_text(result: dict) -> str:
    """Flatten MCP content blocks to text (text blocks only; other block types
    are summarized, never dumped)."""
    parts: list[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        else:
            parts.append(f"[{block.get('type') or 'content'}]")
    return "\n".join(p for p in parts if p).strip()


def call_tool(org_id: str, name: str, arguments: dict | None, *, meta: dict | None = None,
              live: bool = False) -> dict:
    """Invoke one Cedric tool (MCP tools/call). ``name`` is the BARE tool name
    (no prefix). ``live`` picks the hot-path timeout budget (~2s) vs off-path
    (~90s) and is informational for the caller.

    Returns a normalized dict (never raises):
      {ok, text, structured, truncated, is_error, approval_required, summary,
       proposed_action, error_kind}
    Exactly one of ok / is_error / approval_required / error_kind is the headline."""
    if not enabled():
        return {"error_kind": "disabled", "text": "the tool bridge is off"}
    timeout = settings.cedric_mcp_live_timeout_s if live else settings.cedric_mcp_offpath_timeout_s
    params: dict[str, Any] = {"name": name, "arguments": dict(arguments or {})}
    if meta:
        params["_meta"] = meta

    def _once() -> tuple[Any, None]:
        sid = _ensure_session(org_id, timeout=timeout)
        result, _sid = _rpc(org_id, "tools/call", params, timeout=timeout,
                            session_id=sid, _id=3)
        return result, None

    try:
        result, _ = _once()
    except McpError as e:
        if e.kind in {"transport", "auth"}:  # stale session → one fresh retry
            _invalidate(org_id)
            try:
                result, _ = _once()
            except McpError as e2:
                return {"error_kind": e2.kind, "text": e2.message}
        else:
            return {"error_kind": e.kind, "text": e.message}
    if not isinstance(result, dict):
        return {"error_kind": "rpc_error", "text": "empty tool result"}

    structured = result.get("structuredContent")
    structured = structured if isinstance(structured, dict) else {}
    # Approval-gated write (policy outcome, isError=false): captured, not executed.
    if str(structured.get("status") or "") == "approval_required":
        return {
            "approval_required": True,
            "summary": str(structured.get("summary") or ""),
            "proposed_action": structured.get("proposed_action") or {},
            "structured": structured,
        }
    text = _content_text(result)
    if result.get("isError"):
        return {"is_error": True, "text": text or "the tool reported an error"}
    return {
        "ok": True,
        "text": text,
        "structured": structured,
        "truncated": bool(structured.get("result_truncated")),
    }


def result_to_model_text(res: dict) -> str:
    """Collapse a call_tool result to the single string the LLM tool loop feeds
    back to the model. Honesty-preserving: an approval-gated write reads as
    'queued', never 'done'; failures read as failures; truncation is flagged."""
    if res.get("ok"):
        text = res.get("text") or "(the tool returned no content)"
        return text + ("\n[result truncated]" if res.get("truncated") else "")
    if res.get("approval_required"):
        s = res.get("summary") or "this action"
        return f"queued for the owner to approve: {s} (not done yet)"
    if res.get("is_error"):
        return f"that tool failed: {res.get('text') or 'unknown error'}"
    kind = res.get("error_kind") or "error"
    friendly = {
        "not_linked": "the Slack agent isn't linked for this org yet",
        "unknown_tool": "there is no such tool for this org",
        "tool_not_connected": "that tool isn't connected for this org",
        "scope_missing": "the connected account lacks permission for that",
        "invalid_args": "I called that tool with the wrong arguments",
        "rate_limited": "that tool is rate-limited right now; try again shortly",
        "timeout": "that lookup took too long, so I skipped it",
        "auth": "the tool bridge rejected our credentials",
        "disabled": "the tool bridge is off",
    }.get(kind, f"that tool is unavailable ({kind})")
    return friendly
