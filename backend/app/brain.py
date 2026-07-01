"""The reasoning layer.

Two jobs:
  1. answer_question()  — live, grounded, cited answers when the avatar is called.
  2. post_meeting()     — summary + gap checklist + draft follow-up email.

Everything is grounded in retrieved process docs. The model is instructed to
say so when context is insufficient, and to return a confidence the speak-gate
can threshold on. That confidence + citation pair is the trust layer.

The actual model is pluggable (see llm.py / BRAIN_PROVIDER):
  - anthropic — Claude, best quality.
  - ollama    — a local model, free.
  - stub      — no model at all: deterministic extractive answers built from the
                retrieved chunks. Lets the whole pipeline run offline for free.
"""
from __future__ import annotations

import json
import re

from . import llm
from .avatars import Avatar
from .config import settings
from .rag import retrieve, Retrieved

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# Words in a meeting line that hint at an actionable / gap-prone item (stub mode).
_ACTION_HINTS = re.compile(
    r"\b(need|needs|should|must|todo|to-do|follow[\s-]?up|assign|approv|owner|"
    r"deadline|by (monday|tuesday|wednesday|thursday|friday|next week|eod)|"
    r"missing|pending|waiting|blocked|review)\b",
    re.IGNORECASE,
)


def _format_context(chunks: list[Retrieved]) -> str:
    blocks = []
    for i, c in enumerate(chunks, 1):
        blocks.append(f"[{i}] (source: {c.source} — {c.section})\n{c.text}")
    return "\n\n".join(blocks)


def effective_provider() -> str:
    """The provider we'll actually use.

    If the brain is set to 'anthropic' but no key is present yet, we transparently
    fall back to the free offline stub — so the demo works the moment you clone it
    and upgrades to real Claude the moment you paste a key. No crash in between.
    """
    p = settings.brain_provider.lower()
    if p == "anthropic" and not settings.anthropic_api_key:
        return "stub"
    return p


def _is_stub() -> bool:
    return effective_provider() == "stub"


# ─────────────────────────── live answers ───────────────────────────
ANSWER_SYSTEM = """{persona}

You are a callable AI process expert that has been invited into a live work \
meeting. You speak ONLY from the company process documents provided as context. \
You are concise: this is spoken aloud, so answer in 1-3 short sentences a person \
can absorb by ear.

Rules:
- Use ONLY the provided context. Do not invent steps, owners, or approvals.
- If the context does not contain the answer, say so plainly and do not guess.
- Cite the source document you relied on.
- Spoken style: no markdown, no bullet symbols, no headings.

Return ONLY a JSON object:
{{
  "answer": "<what the avatar should say, spoken style>",
  "citations": ["<source filename>", ...],
  "confidence": <0.0-1.0, how well the context supports this answer>,
  "sufficient_context": <true|false>
}}"""


def answer_question(
    avatar: Avatar, question: str, *, history: str = "", k: int = 4
) -> dict:
    """Retrieve + answer for one avatar. Returns answer/citations/confidence.

    `history` is the recent meeting conversation (last few "Speaker: line" turns)
    so the avatar understands *this* discussion, not just the isolated question.
    """
    chunks = retrieve(avatar, question, k=k)

    if _is_stub():
        result = _stub_answer(chunks)
    else:
        convo = f"Recent meeting conversation:\n{history}\n\n" if history.strip() else ""
        raw = llm.complete(
            ANSWER_SYSTEM.format(persona=avatar.persona_prompt),
            (
                f"Company process context:\n\n{_format_context(chunks)}\n\n"
                f"{convo}"
                f"Someone in the meeting asked:\n{question}\n\n"
                "Respond with the JSON object only."
            ),
            max_tokens=400,
        )
        result = _parse_json(raw)

    result.setdefault("citations", [c.source for c in chunks[:1]])
    result.setdefault("confidence", 0.0)
    result.setdefault("sufficient_context", False)
    result["retrieved"] = [
        {"source": c.source, "section": c.section, "score": round(c.score, 3)}
        for c in chunks
    ]
    return result


# ───────────────────────── post-meeting ─────────────────────────────
POSTMEETING_SYSTEM = """You analyze a meeting transcript against company \
process knowledge. Produce a crisp post-meeting artifact a team can act on.

Detect PROCESS GAPS, specifically any of: missing owner, missing deadline, \
missing approval, missing required document, unresolved blocker. Only flag a \
gap if it is genuinely implied by the discussion; do not pad the list.

Return ONLY a JSON object:
{
  "summary": "<3-5 sentence plain summary of what was discussed and decided>",
  "checklist": [
    {"item": "<action>", "owner": "<name or 'UNASSIGNED'>", "gap_type": "<owner|deadline|approval|document|blocker|none>"}
  ],
  "follow_up_email": {
    "subject": "<subject line>",
    "body": "<short professional email body summarizing decisions and next steps>"
  }
}"""


def post_meeting(avatar: Avatar, transcript_text: str, *, k: int = 6) -> dict:
    """Summary + gap checklist + draft follow-up email for a finished meeting."""
    # Ground gap-detection in the actual process docs.
    chunks = retrieve(
        avatar, transcript_text[-3000:] or "process steps owners approvals", k=k
    )

    if _is_stub():
        return _stub_post_meeting(avatar, transcript_text)

    raw = llm.complete(
        POSTMEETING_SYSTEM,
        (
            f"Relevant company process context:\n\n{_format_context(chunks)}\n\n"
            f"Meeting transcript:\n\n{transcript_text}\n\n"
            "Respond with the JSON object only."
        ),
        max_tokens=1200,
    )
    return _parse_json(raw)


# ─────────────────── stub (free, offline) reasoning ─────────────────
def _stub_answer(chunks: list[Retrieved]) -> dict:
    """Deterministic extractive answer: quote the best-matching process chunk.

    No model involved — this proves the retrieve→answer→cite pipeline for free.
    """
    if not chunks or chunks[0].score < 0.12:
        return {
            "answer": (
                "I don't have that in the process documents I was given, "
                "so I can't answer confidently."
            ),
            "citations": [],
            "confidence": 0.0,
            "sufficient_context": False,
        }
    top = chunks[0]
    sentences = [s.strip() for s in _SENTENCE.split(top.text) if s.strip()]
    snippet = " ".join(sentences[:2]) if sentences else top.text[:240]
    return {
        "answer": f"Per {top.source} ({top.section}): {snippet}",
        "citations": [top.source],
        "confidence": round(min(0.9, 0.4 + top.score), 2),
        "sufficient_context": True,
    }


def _stub_post_meeting(avatar: Avatar, transcript_text: str) -> dict:
    """Deterministic post-meeting artifact from simple transcript heuristics."""
    lines = [ln.strip() for ln in transcript_text.splitlines() if ln.strip()]
    speakers = []
    for ln in lines:
        who = ln.split(":", 1)[0].strip() if ":" in ln else ""
        if who and who not in speakers:
            speakers.append(who)

    checklist = []
    for ln in lines:
        body = ln.split(":", 1)[1].strip() if ":" in ln else ln
        if _ACTION_HINTS.search(body):
            gap = "none"
            low = body.lower()
            if "approv" in low:
                gap = "approval"
            elif "owner" in low or "assign" in low or "who" in low:
                gap = "owner"
            elif "deadline" in low or "by " in low:
                gap = "deadline"
            elif "document" in low or "doc " in low or "form" in low:
                gap = "document"
            elif "block" in low or "waiting" in low or "pending" in low:
                gap = "blocker"
            checklist.append(
                {"item": body[:160], "owner": "UNASSIGNED", "gap_type": gap}
            )

    summary = (
        f"{avatar.name} sat in on a meeting with {len(speakers)} participant(s) "
        f"({', '.join(speakers) or 'unknown'}) across {len(lines)} lines. "
        f"{len(checklist)} potential action item(s)/process gap(s) were detected "
        "by keyword heuristics (offline stub mode — enable a real brain for a "
        "true summary)."
    )
    return {
        "summary": summary,
        "checklist": checklist[:12],
        "follow_up_email": {
            "subject": f"Follow-up & open items from today's session ({avatar.name})",
            "body": (
                "Hi team,\n\nThanks for the discussion. Below are the open items "
                "and possible process gaps flagged during the meeting:\n\n"
                + (
                    "\n".join(f"- {c['item']} (owner: {c['owner']})" for c in checklist[:12])
                    or "- No explicit action items detected."
                )
                + f"\n\nBest,\n{avatar.name}"
            ),
        },
    }


def _parse_json(text: str) -> dict:
    """Tolerant JSON extraction — strips ``` fences and surrounding prose."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"answer": text, "confidence": 0.0, "sufficient_context": False}
