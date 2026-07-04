"""The 'act' layer — small tools Laura can CALL to *do* things, not just recall.

Retrieval (rag.py) makes Laura well-read; these tools make her able to act:
compute a number, reason about a deadline, or check a record in a system. The
LLM decides when to call one (OpenAI/Groq 'tools' function-calling); llm.py runs
the call and feeds the result back so the final spoken answer is concrete.

Deliberately tiny, deterministic, and KEY-FREE so the demo runs offline:
  - calculator    : safe arithmetic (percentages, totals, per-seat costs, splits)
  - date_math     : today's date, or how many days until a deadline
  - lookup_record : a SYNTHETIC in-memory 'system of record' (demo data only)

Safety: the calculator parses an AST and only allows numeric arithmetic (never
eval()); lookup_record returns SYNTHETIC data only — no real PII, matching the
project's demo-safety rules. To add a real integration later (CRM, calendar,
DB), implement it as one more function + spec here and register it below.
"""
from __future__ import annotations

import ast
import json
import operator
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
]

_DISPATCH = {
    "calculator": calculator,
    "date_math": date_math,
    "lookup_record": lookup_record,
}


def dispatch(name: str, args: dict) -> str:
    """Run a tool by name with keyword args; always returns a string for the model."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"error: unknown tool '{name}'"
    try:
        return str(fn(**(args or {})))
    except TypeError as e:
        return f"error: bad arguments for '{name}' ({e})"
