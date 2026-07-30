"""OpenClaw action route — execute an approved action via the local OpenClaw
gateway agent instead of the in-process vendor clients.

The dev seam for moving the action plane onto OpenClaw (the agent holds the
tool connections and can carry out multi-step actions). This module is
transport-only: ``executor.execute_approved`` still owns the type gating and
the ledger/Cedric provenance writeback; this module performs exactly one
action through the gateway and soft-returns the same ``{"ok": bool, ...}``
shape the vendor clients use (plus ``receipt``/``detail`` for the ledger
line). Never raises.

Runs at finalize/approval only — NEVER on the transcript→speak live path (a
CLI round-trip through the gateway costs seconds, not milliseconds). Off by
default (``OPENCLAW_EXECUTOR``): prod and the key-free demo never shell out.
The action payload can carry meeting-derived content, so nothing here is ever
printed or logged — the only outputs are the returned dict fields.
"""
from __future__ import annotations

import json
import subprocess

from ..config import settings


def enabled() -> bool:
    """Whether the OpenClaw route is active (the flag is the single switch)."""
    return bool(settings.openclaw_executor)


def _receipt_from_text(text: str) -> dict:
    """Parse the mandated JSON receipt out of the agent's reply text."""
    t = (text or "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return {"ok": False, "error": "no JSON receipt in agent reply"}
    try:
        obj = json.loads(t[start : end + 1])
    except ValueError:
        return {"ok": False, "error": "unparseable agent receipt"}
    if not isinstance(obj, dict) or "ok" not in obj:
        return {"ok": False, "error": "malformed agent receipt"}
    if not obj.get("ok"):
        detail = str(obj.get("detail") or "agent reported failure")
        return {"ok": False, "error": detail, "detail": detail}
    return {
        "ok": True,
        "receipt": str(obj.get("receipt") or ""),
        "detail": str(obj.get("detail") or ""),
    }


def run(org_id: str, action_id: str, action: dict) -> dict:
    """Execute one approved action through the gateway agent. Soft-returns —
    every failure mode (missing binary, timeout, garbage output, agent-side
    failure) becomes ``{"ok": False, "error": ...}``."""
    prompt = (
        "You are the action executor for the Laura avatar platform "
        f"(org {org_id}). Execute this approved action now, using your "
        "connected tools:\n"
        f"{json.dumps(action, ensure_ascii=False)}\n"
        "Rules: perform exactly this one action — do not invent recipients, "
        "content, or extra steps. If you cannot perform it with your current "
        "tools/connections, do NOT improvise; report failure. Reply with ONLY "
        'a JSON object: {"ok": true|false, "receipt": "<url or id of what you '
        'created/sent, or empty>", "detail": "<one short sentence on the '
        'outcome>"}'
    )
    agent = settings.openclaw_agent_id
    cmd = [
        settings.openclaw_bin, "agent",
        "--agent", agent,
        # One session per action: no cross-action context bleed, and a rerun
        # of the same action lands in the same session for the audit trail.
        "--session-key", f"agent:{agent}:laura-action-{action_id or 'adhoc'}",
        "--message", prompt,
        "--timeout", str(int(settings.openclaw_timeout_s)),
        "--json",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=settings.openclaw_timeout_s + 30,
        )
    except Exception as e:  # noqa: BLE001 — TimeoutExpired, missing binary, …
        return {"ok": False, "error": f"openclaw unreachable ({type(e).__name__})"}
    out = proc.stdout or ""
    start = out.find("{")
    if proc.returncode != 0 or start < 0:
        return {"ok": False, "error": f"openclaw CLI failed (rc={proc.returncode})"}
    try:
        data = json.loads(out[start:])
    except ValueError:
        return {"ok": False, "error": "unparseable openclaw CLI output"}
    if not isinstance(data, dict) or data.get("status") != "ok":
        status = data.get("status") if isinstance(data, dict) else None
        return {"ok": False, "error": f"agent run {status or 'error'}"}
    result = data.get("result") or {}
    meta = result.get("meta") or {}
    text = meta.get("finalAssistantVisibleText") or ""
    if not text:
        payloads = result.get("payloads") or []
        text = "\n".join(
            str(p.get("text") or "") for p in payloads if isinstance(p, dict)
        )
    return _receipt_from_text(text)
