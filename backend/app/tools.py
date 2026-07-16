"""The 'act' layer — small tools Laura can CALL to *do* things, not just recall.

Retrieval (rag.py) makes Laura well-read; these tools make her able to act:
compute a number, reason about a deadline, or check a record in a system. The
LLM decides when to call one (OpenAI/Groq 'tools' function-calling); llm.py runs
the call and feeds the result back so the final spoken answer is concrete.

Deliberately tiny, deterministic, and KEY-FREE so the demo runs offline:
  - calculator    : safe arithmetic (percentages, totals, per-seat costs, splits)
  - date_math     : today's date, or how many days until a deadline
  - lookup_record : a SYNTHETIC in-memory 'system of record' (demo data only)
  - queue_action  : capture a requested action for approval AFTER the call
                    (durable local capture; callback delivery is off-path)

Safety: the calculator parses an AST and only allows numeric arithmetic (never
eval()); lookup_record returns SYNTHETIC data only — no real PII, matching the
project's demo-safety rules. To add a real integration later (CRM, calendar,
DB), implement it as one more function + spec here and register it below.
"""
from __future__ import annotations

import ast
import json
import operator
import uuid
from datetime import date, datetime

# ───────────────────────────── calculator ─────────────────────────────
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("only plain numeric arithmetic is allowed")


def calculator(expression: str = "") -> str:
    """Evaluate a pure arithmetic expression. Safe: AST-parsed, never eval()."""
    try:
        val = _eval_node(ast.parse(expression, mode="eval").body)
    except Exception as e:  # noqa: BLE001 — surface the reason to the model
        return f"error: could not evaluate '{expression}' ({e})"
    if isinstance(val, float):
        val = int(val) if val.is_integer() else round(val, 4)
    return str(val)


# ───────────────────────────── date_math ──────────────────────────────
def date_math(operation: str = "today", date: str = "") -> str:  # noqa: A002
    """operation='today' -> today's date; 'days_until' -> days to a YYYY-MM-DD date."""
    from datetime import date as _date  # local alias (param shadows the import)

    today = _date.today()
    if operation == "today":
        return f"{today.isoformat()} ({today.strftime('%A')})"
    if operation == "days_until":
        try:
            target = datetime.strptime(date.strip(), "%Y-%m-%d").date()
        except ValueError:
            return f"error: date must be YYYY-MM-DD, got '{date}'"
        delta = (target - today).days
        if delta > 0:
            return f"{delta} day(s) from today until {target.isoformat()}"
        if delta < 0:
            return f"{-delta} day(s) ago ({target.isoformat()} is in the past)"
        return f"{target.isoformat()} is today"
    return f"error: unknown operation '{operation}' (use 'today' or 'days_until')"


# ─────────────────────────── lookup_record ────────────────────────────
# A SYNTHETIC system of record — stands in for a CRM/DB integration so the
# pattern is demonstrable offline. Renewal dates are chosen so date_math can
# compose with a lookup ("how many days until Acme's renewal?").
_RECORDS: dict[str, dict] = {
    "ACME-1001": {
        "customer": "Acme Corp", "plan": "Growth", "seats": 25,
        "mrr_usd": 1250, "renewal": "2026-09-30", "status": "active", "owner": "Priya",
    },
    "GLOBEX-2007": {
        "customer": "Globex", "plan": "Enterprise", "seats": 120,
        "mrr_usd": 9600, "renewal": "2026-07-31", "status": "active", "owner": "Marco",
    },
    "INITECH-3050": {
        "customer": "Initech", "plan": "Starter", "seats": 8,
        "mrr_usd": 240, "renewal": "2026-08-15", "status": "trial", "owner": "Dana",
    },
}


def lookup_record(record_id: str = "", query: str = "") -> str:
    """Look up a (demo) customer account by id (e.g. 'ACME-1001') or customer name."""
    rid = (record_id or "").strip().upper()
    if rid and rid in _RECORDS:
        return json.dumps({"id": rid, **_RECORDS[rid]})
    q = (query or record_id or "").strip().lower()
    if q:
        for rid, rec in _RECORDS.items():
            if q in rec["customer"].lower() or q in rid.lower():
                return json.dumps({"id": rid, **rec})
    known = ", ".join(f"{k} ({v['customer']})" for k, v in _RECORDS.items())
    return f"error: no record for '{record_id or query}'. Known demo accounts: {known}"


# ─────────────────────────── queue_action ─────────────────────────────
# The "do something" bridge — a PLATFORM tool every avatar gets. A live
# meeting is where actions are REQUESTED, never where they execute (execution
# lives behind an approval after the call — e.g. Cedric's Slack cards, or
# Laura's autopilot follow-up). When someone asks the avatar to DO something
# ("send the recap", "book a follow-up"), this captures {action, owner, due}
# on the live session and in local SQLite. No network occurs on the live path;
# the callback outbox worker delivers asynchronously. Captured items are merged into the
# post-meeting artifact's actions[] at finalize (main._finalize_session) and,
# for orchestrated sessions, announced immediately via the action.requested
# webhook (fired OFF the live path by cedric.notify_action_requested).
def capture_action_once(
    session,
    action: str,
    owner: str = "",
    due: str = "",
    *,
    source_event_key: str = "",
    source_fingerprint: str = "",
    dedupe_window_seconds: float = 30.0,
) -> tuple[dict, bool]:
    """Capture once even when Recall concurrently retries the same final."""
    proposed = {
        "action_id": uuid.uuid4().hex[:16],
        "action": " ".join((action or "").split())[:300],
        "owner": " ".join((owner or "").split())[:100],
        "due": " ".join((due or "").split())[:100],
    }
    from . import outbox

    item, created, _outbox_id = outbox.persist_action_capture_once(
        session,
        proposed,
        source_event_key=source_event_key,
        source_fingerprint=source_fingerprint,
        dedupe_window_seconds=dedupe_window_seconds,
    )
    if not created:
        return item, False

    queued = getattr(session, "queued_actions", None)
    if queued is None:
        queued = []
        session.queued_actions = queued
    queued.append(item)
    try:
        from .cedric import notify_action_requested

        notify_action_requested(session, session.bot_id, item)
    except Exception:  # noqa: BLE001 — durable worker owns delivery
        pass
    return item, True


def capture_action(session, action: str, owner: str = "", due: str = "") -> dict:
    """Compatibility capture primitive for tool calls without Recall identity."""
    item, _created = capture_action_once(session, action, owner, due)
    return item


def extend_action_once(
    session,
    item: dict,
    fragment: str,
    *,
    source_event_key: str = "",
    source_fingerprint: str = "",
    dedupe_window_seconds: float = 30.0,
) -> tuple[dict, bool]:
    """Durably append one ASR continuation; retries return the same item."""
    from . import outbox

    canonical, extended = outbox.extend_action_capture_once(
        session,
        item,
        fragment,
        source_event_key=source_event_key,
        source_fingerprint=source_fingerprint,
        dedupe_window_seconds=dedupe_window_seconds,
    )
    if isinstance(item, dict):
        item.clear()
        item.update(canonical)
    return canonical, extended


def queue_action(
    action: str = "", owner: str = "", due: str = "", session=None
) -> str:
    """Capture a requested action durably; delivery remains asynchronous."""
    if not (action or "").strip():
        return "error: 'action' is required — one short line saying what should be done"
    if session is None:
        # No live meeting session behind this conversation (e.g. the direct
        # web-avatar page): be honest — nothing gets queued here.
        return (
            "note: there is no live meeting session, so nothing was queued — "
            "tell the person you can only queue actions during a meeting."
        )
    try:
        capture_action(session, action, owner, due)
    except Exception as exc:
        from .outbox import ActionCaptureClosed, OutboxUnavailable
        if isinstance(exc, ActionCaptureClosed):
            return (
                "error: this meeting is already finalizing, so the action "
                "was not queued."
            )
        if isinstance(exc, OutboxUnavailable):
            return (
                "error: I couldn't save that action safely — please try again "
                "in a moment."
            )
        raise
    return "Noted — I'll queue that for approval in Slack right after the call."


# ─────────────────────── registry (OpenAI/Groq format) ─────────────────
# The `tools` array sent to the model. Descriptions matter: they're how the
# model decides *when* to call each tool — keep them concrete.
TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": (
                "Evaluate an arithmetic expression. Use for ANY math a spoken answer "
                "needs: percentages (0.15*25000), totals, per-seat cost (mrr/seats), "
                "annualizing (mrr*12), splits. Returns the numeric result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Pure arithmetic using numbers and + - * / % ** ( ).",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "date_math",
            "description": (
                "Reason about dates. operation='today' returns today's date; "
                "operation='days_until' with date='YYYY-MM-DD' returns how many days "
                "remain until that date (use for deadlines/renewals)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["today", "days_until"]},
                    "date": {
                        "type": "string",
                        "description": "Target date as YYYY-MM-DD (required for days_until).",
                    },
                },
                "required": ["operation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_record",
            "description": (
                "Look up a customer account in the system of record by account id "
                "(e.g. 'ACME-1001') or by customer name. Returns plan, seats, MRR, "
                "renewal date, status and owner. Use when asked about a specific "
                "customer/account."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string", "description": "Account id, e.g. ACME-1001."},
                    "query": {"type": "string", "description": "Customer name to search."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_action",
            "description": (
                "Queue a REQUESTED action for approval after the call. Use whenever "
                "someone asks you to DO something: send an email or recap, schedule "
                "or book a follow-up, create a ticket or doc, check on something, "
                "remind someone, invite someone. This only captures the request — "
                "it is executed AFTER the meeting behind an approval, never during "
                "the call. NEVER claim the action was already done; confirm it is "
                "queued for approval right after the call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "One short line: what should be done.",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Who should own or do it (a name), if stated.",
                    },
                    "due": {
                        "type": "string",
                        "description": "Deadline or timeframe if stated, e.g. 'Friday' or '2026-07-15'.",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_capabilities",
            "description": (
                "List what YOU can actually do in this meeting: native tools, "
                "the Slack-agent tools that are connected for this org, and "
                "what is NOT connected. Use when asked \"what can you do?\" or "
                "before promising any action you are not sure about."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upcoming_meetings",
            "description": (
                "The owner's upcoming Google Calendar meetings (read-only "
                "snapshot taken at session start). Use for \"what's on my/our "
                "calendar\", \"when is my next meeting\", or \"do I have a "
                "meeting with X\"."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": (
                "Check whether a specific capability/tool exists for this org "
                "(e.g. 'notion', 'github', 'calendar'): where it runs, whether "
                "it is connected, and whether it needs approval. Use BEFORE "
                "claiming you can or cannot do something with an external tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Tool or capability keyword, e.g. 'notion'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
]

def list_capabilities(session=None) -> str:
    """The org-scoped tool brief for THIS session — what the avatar can do
    natively, what runs via the Slack agent, what is NOT connected. Reads the
    snapshot assembled at session start (tool_registry.assemble) — zero
    network on the live path."""
    from . import tool_registry

    reg = getattr(session, "tool_registry", None) if session else None
    return tool_registry.brief(reg) or "capability list unavailable for this session"


def search_tools(query: str = "", session=None) -> str:
    """Look up whether a capability exists, where it runs (native vs Slack
    agent), whether it is connected, and whether it needs approval. Snapshot
    search only — never a live call."""
    from . import tool_registry

    reg = getattr(session, "tool_registry", None) if session else None
    return tool_registry.search(reg, query)


def upcoming_meetings(session=None) -> str:
    """The owner's upcoming-calendar snapshot for THIS session — assembled at
    session start (google_client.calendar_brief), zero network on the live
    path. "" from the assembler means no Google connected for the org."""
    brief = getattr(session, "calendar_brief", "") if session else ""
    return brief or (
        "no calendar is connected for this meeting's org — connect Google "
        "in the dashboard to give me calendar sight"
    )


_DISPATCH = {
    "calculator": calculator,
    "date_math": date_math,
    "lookup_record": lookup_record,
    "queue_action": queue_action,
    "list_capabilities": list_capabilities,
    "search_tools": search_tools,
    "upcoming_meetings": upcoming_meetings,
}

# Tools that receive the live session (to capture onto it). Everything else
# keeps its plain signature — the session seam is strictly additive.
_SESSION_TOOLS = {
    "queue_action", "list_capabilities", "search_tools", "upcoming_meetings",
}


def specs_for(session, *, live: bool = True) -> list[dict]:
    """The function-calling specs offered to the model for THIS session: the
    native TOOL_SPECS plus any Cedric tools discovered at join (the MCP bridge).
    On the LIVE meeting path only read-only + fast Cedric tools are offered —
    the latency contract (Handshake v3). When the bridge is off or nothing was
    discovered, this is exactly TOOL_SPECS."""
    specs = list(TOOL_SPECS)
    reg = getattr(session, "tool_registry", None) if session else None
    mcp_tools = reg.get("cedric_mcp") if isinstance(reg, dict) else None
    if mcp_tools:
        from . import cedric_mcp

        specs += cedric_mcp.to_function_specs(mcp_tools, live=live)
    return specs


def _dispatch_cedric(name: str, args: dict, session, *, live: bool) -> str:
    """Route a prefixed Cedric tool call through the MCP bridge. An approval-
    gated write is CAPTURED onto the session (approve queue) and reported as
    queued — never executed here, never claimed done."""
    from . import cedric_mcp

    org_id = str(getattr(session, "org_id", "") or "") if session else ""
    if not org_id:
        return "error: no org is attached to this session, so I can't use that tool"
    bare = name[len(cedric_mcp.TOOL_PREFIX):]
    # The avatar acts autonomously on the live read path → actor='avatar', no
    # human user_ref (PII: never an email); ref ties it to this meeting for audit.
    meta = {"actor": "avatar", "source": "meeting" if live else "dashboard"}
    bot_id = str(getattr(session, "bot_id", "") or "") if session else ""
    if bot_id:
        meta["ref"] = bot_id
    res = cedric_mcp.call_tool(org_id, bare, args or {}, meta=meta, live=live)
    if res.get("approval_required") and session is not None:
        try:
            queue_action(action=res.get("summary") or bare, session=session)
        except Exception:  # noqa: BLE001 — the spoken "queued" is enough; capture is best-effort
            pass
    return cedric_mcp.result_to_model_text(res)


def dispatch(name: str, args: dict, session=None, *, live: bool = True) -> str:
    """Run a tool by name with keyword args; always returns a string for the model.

    `session` (optional) is the live store.Session — threaded only into the
    tools listed in _SESSION_TOOLS so they can capture onto it. A ``cedric__``-
    prefixed name is a Cedric tool and routes through the MCP bridge.
    """
    if name.startswith("cedric__"):
        return _dispatch_cedric(name, args or {}, session, live=live)
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"error: unknown tool '{name}'"
    try:
        if name in _SESSION_TOOLS:
            return str(fn(**(args or {}), session=session))
        return str(fn(**(args or {})))
    except TypeError as e:
        return f"error: bad arguments for '{name}' ({e})"


def dispatch_for(session, *, live: bool = True):
    """`dispatch` bound to a live session — the same (name, args) callable the
    LLM tool loop expects, but session-aware tools capture onto the session and
    Cedric tools route through the MCP bridge. dispatch_for(None) behaves exactly
    like plain dispatch. ``live`` selects the latency budget for Cedric calls."""

    def _dispatch(name: str, args: dict) -> str:
        return dispatch(name, args, session=session, live=live)

    return _dispatch
