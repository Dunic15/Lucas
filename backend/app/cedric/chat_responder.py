"""Native Cedric replies for the dashboard chat.

The chat tab's contract is honest conversation: a sent message should get an
answer. The full Cedric orchestrator answers over the per-org events door
(chat.message -> external runtime -> POST /org/chat); until a deployment
configures that relay, THIS module is Cedric — the same brain provider that
answers in meetings, prompted with Cedric's persona plus a compact, PII-safe
view of the org's real state (pending actions, connections, recent meetings),
so "what's waiting on me?" gets a grounded answer instead of vibes.

Deliberate boundaries (v1):
- Replies never EXECUTE anything. Approvals stay on the canonical doors —
  the chat card buttons and the Action Center hit those doors themselves.
- Context is artifact metadata only: action items, owners, statuses, summary
  first-lines. Never transcripts (PII stays in the meeting store) and never
  logged.
- Key-free (stub provider) answers a deterministic canned line so the demo
  and the suite work without keys.
"""
from __future__ import annotations

from .. import avatars, ledger, store

# Statuses that mean an action no longer waits on anyone.
_SETTLED = ("done", "rejected", "failed")

_STUB_PREFIX = "Got it — noted."

_CHAT_FRAME = """You are Cedric, answering in the ORG DASHBOARD CHAT — typed
messages, not a live meeting. Keep replies short (1-3 sentences), warm and
concrete. You can see the org's real state below; ground answers in it and
say plainly when something isn't in view. You cannot run tools or execute
actions from this chat: work agreed in meetings lands in the Action Center,
where a human approves it (action cards posted here carry their own
Approve/Reject buttons). Never claim something ran unless its status below
says done. If a tool isn't connected yet, point at the Connections page.

Persona:
{persona}

Org state (metadata only — no transcripts):
{context}"""


def _org_context(org: str) -> str:
    """A compact, PII-safe brief of the org: connections, open actions,
    recent meetings. Artifact metadata only — transcripts never leave the
    meeting store. Every read is best-effort: a failed source just drops
    out of the brief."""
    lines: list[str] = []
    try:
        google = "connected" if store.get_org_oauth(org) else "not connected"
    except Exception:  # noqa: BLE001
        google = "unknown"
    try:
        from .. import asana_client

        asana = "connected" if asana_client.connected(org) else "not connected"
    except Exception:  # noqa: BLE001
        asana = "unknown"
    lines.append(f"Connections: Google {google}; Asana {asana}.")

    try:
        arts = store.list_artifacts(org)[:6]
    except Exception:  # noqa: BLE001
        arts = []
    actions: list[dict] = []
    for art in arts:
        for a in (art.get("artifact") or {}).get("actions") or []:
            if isinstance(a, dict) and (a.get("item") or "").strip():
                actions.append(a)
    ids = [str(a.get("action_id") or "") for a in actions if a.get("action_id")]
    statuses: dict = {}
    if ids:
        try:
            statuses = ledger.action_statuses(ids[:24], org_id=org) or {}
        except Exception:  # noqa: BLE001
            statuses = {}
    shown = 0
    for a in actions:
        if shown >= 8:
            break
        aid = str(a.get("action_id") or "")
        status = (statuses.get(aid) or {}).get("status") or "pending approval"
        owner = (a.get("owner") or "").strip() or "unassigned"
        lines.append(
            f"- Action [{status}] {str(a.get('item') or '')[:120]} (owner: {owner})"
        )
        shown += 1
    if not shown:
        lines.append("- No captured actions on file.")

    for art in arts[:3]:
        summary = str((art.get("artifact") or {}).get("summary") or "").strip()
        if summary:
            lines.append(f"- Meeting note: {summary[:140]}")
    return "\n".join(lines)


def _history(org: str, limit: int = 10) -> str:
    """The last few chat turns, oldest first — continuity, not archive."""
    try:
        msgs = store.list_chat_messages(org)[-limit:]
    except Exception:  # noqa: BLE001
        return ""
    out = []
    for m in msgs:
        who = "Cedric" if m.get("sender") == "cedric" else "User"
        body = str(m.get("body") or "").strip()
        if body:
            out.append(f"{who}: {body[:300]}")
    return "\n".join(out)


def build_reply(org: str, text: str) -> str:
    """One Cedric chat reply for ``text``. Stub provider = deterministic
    canned line (key-free demo/suite); real providers get persona + org
    brief + recent turns."""
    from ..brain import engine as brain  # lazy: brain resolves keyless configs to the stub

    if brain.effective_provider() == "stub":
        return (
            f"{_STUB_PREFIX} On this key-free run I answer canned lines, "
            "but your message is saved — actions and approvals live in the "
            "Action Center."
        )
    try:
        persona = avatars.load("cedric").persona_prompt
    except Exception:  # noqa: BLE001
        persona = "You are Cedric, the org's AI colleague."
    system = _CHAT_FRAME.format(persona=persona, context=_org_context(org))
    user = (_history(org) + f"\nUser: {text}\nCedric:").lstrip("\n")
    from .. import llm  # lazy: keeps module import light for pure-unit tests

    reply = (llm.complete(system, user, max_tokens=400) or "").strip()
    return reply or "I'm here — say that again? I didn't produce an answer."


def respond_and_store(org: str, text: str) -> bool:
    """Generate Cedric's reply and append it to the org channel. Never
    raises (fired off the request path); a generation failure stores an
    honest apology instead of silence."""
    try:
        reply = build_reply(org, text)
    except Exception:  # noqa: BLE001 — never let the channel go silent
        reply = "Sorry — I hit an error answering that. Try me again in a moment."
    row = store.add_chat_message(org, "cedric", body=reply, sender_label="Cedric")
    return row is not None
