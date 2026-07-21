"""Company-aware checkpoint narration; deterministic, cited, on the EXISTING
speech queue.

For the MVP this is deterministic per-checkpoint narration (not a full
autonomous narration loop; documented as such). For each manifest checkpoint
it retrieves the relevant company material through ContextResolver (the ONE
retrieval boundary), builds a short cited line using the SELECTED avatar
overlay, and rides Laura's existing ``_make_avatar_speak`` queue with citations
, never a second narration queue. Another org's documents can never appear
(ContextResolver is org+avatar scoped, RLS-isolated).
"""
from __future__ import annotations

from typing import Any, Optional

from ..datafoundation import resolver
from . import manifest

# Which knowledge purpose grounds each checkpoint (from the manifest mapping).
_CHECKPOINT_QUERY = {
    "understand-northstar": "The five onboarding stages",
    "read-acme-status": "Current status",
    "locate-blocked-stage-visually": "Blocker: WMS sandbox credentials",
    "preview-followup": "Follow-up task creation",
    "approve-and-create": "Follow-up task creation",
}


def checkpoint_context(org_id: str, checkpoint_id: str, *,
                       avatar_key: str = "laura",
                       principal_id: str = "") -> dict[str, Any]:
    """Retrieve the cited company material for a checkpoint through the ONE
    boundary. Returns {text, citations}; citations name the Northstar source
    + section. Never another org's documents."""
    query = _CHECKPOINT_QUERY.get(checkpoint_id, "")
    if not query:
        return {"text": "", "citations": []}
    result = resolver.resolve(org_id, avatar_key, query, k=4,
                              principal_id=principal_id,
                              purpose=f"northstar-checkpoint:{checkpoint_id}")
    chunks = result.get("chunks") or []
    citations = []
    for c in chunks[:3]:
        cit = c.get("citation") or {}
        name = str(cit.get("source_name") or "")
        section = str(cit.get("section") or "")
        if name:
            citations.append({"source": name, "section": section})
    top = chunks[0]["text"] if chunks else ""
    return {"text": top[:400], "citations": citations,
            "degraded": bool((result.get("resolution") or {}).get("degraded"))}


async def narrate(session, org_id: str, checkpoint_id: str, *,
                  avatar_key: str = "laura", principal_id: str = "") -> bool:
    """Speak the cited checkpoint line on the EXISTING queue. Returns whether a
    line was delivered. Import of main is lazy to avoid a cycle."""
    ctx = checkpoint_context(org_id, checkpoint_id, avatar_key=avatar_key,
                             principal_id=principal_id)
    if not ctx["text"]:
        return False
    from .. import main as app_main

    return await app_main._make_avatar_speak(
        session, ctx["text"], citations=ctx["citations"], force=True)


def narration_lines(org_id: str, *, avatar_key: str = "laura",
                    principal_id: str = "") -> list[dict]:
    """Deterministic, testable projection of every checkpoint's cited line -
    used by the acceptance path to prove citations WITHOUT a live meeting
    session. Same ContextResolver boundary as narrate()."""
    m = manifest.load()
    out = []
    for cp in m.get("ordered_checkpoints") or []:
        cid = cp["id"]
        if cid not in _CHECKPOINT_QUERY:
            continue
        ctx = checkpoint_context(org_id, cid, avatar_key=avatar_key,
                                 principal_id=principal_id)
        out.append({"checkpoint": cid, "text": ctx["text"],
                    "citations": ctx["citations"]})
    return out
