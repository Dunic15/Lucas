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
import time

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
            model=settings.brain_model_fast,  # latency-critical: fast model
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


# ─────────────────── live answers (streamed) ────────────────────────
# Same trust contract as answer_question, but streamed for low latency: the
# avatar starts speaking the first sentence while the model is still generating
# the rest. The confidence JSON can't stream, so grounding is enforced with a
# SKIP sentinel — the model replies with exactly "SKIP" when the context is
# insufficient, and we stay silent (the streaming equivalent of the confidence
# gate). Citations are known up front from retrieval and spoken at the end.
ANSWER_STREAM_SYSTEM = """{persona}

You are a callable AI process expert invited into a live work meeting. You speak \
ONLY from the company process documents provided as context. This is spoken aloud, \
so answer in 1-3 short sentences a person can absorb by ear.

Rules:
- Use ONLY the provided context. Do not invent steps, owners, or approvals.
- If the context does NOT contain the answer, reply with exactly the single word \
SKIP and nothing else.
- Live transcripts may be imperfect. Infer the likely intent from the recent \
conversation when the wording is noisy, but only answer if the documents still \
support that interpretation.
- If the context partly answers the question, give the useful partial answer and \
say what is not specified instead of skipping.
- Otherwise reply with the spoken answer only: plain text, no markdown, no bullet \
symbols, no headings, no JSON, no preamble."""


def _is_skip(head: str) -> bool:
    """True if `head` is a standalone SKIP sentinel (not a word like 'Skipping')."""
    return head[:4].upper() == "SKIP" and (len(head) == 4 or not head[4].isalpha())


def _retrieval_query(question: str, history: str = "") -> str:
    """Retrieve against the ask plus recent context so vague live speech works."""
    question = (question or "").strip()
    history = (history or "").strip()
    if not history:
        return question
    return f"{history[-1200:]}\n\nCurrent ask: {question}"


def answer_question_stream(avatar: Avatar, question: str, *, history: str = "", k: int = 6):
    """Yield spoken sentences as they are generated. Yields nothing (stays silent)
    when the model judges the context insufficient (SKIP) — same as a failed
    confidence gate in the non-streaming path."""
    _t0 = time.perf_counter()
    chunks = retrieve(avatar, _retrieval_query(question, history), k=k)
    _retrieve_ms = (time.perf_counter() - _t0) * 1000
    citation = chunks[0].source if chunks else ""

    if _is_stub():
        r = _stub_answer(chunks)
        if r.get("sufficient_context"):
            yield r["answer"]
            if citation:
                yield f"— per {citation}"
        return

    convo = f"Recent meeting conversation:\n{history}\n\n" if history.strip() else ""
    system = ANSWER_STREAM_SYSTEM.format(persona=avatar.persona_prompt)
    user = (
        f"Company process context:\n\n{_format_context(chunks)}\n\n"
        f"{convo}"
        f"Someone in the meeting asked:\n{question}\n\n"
        "Answer in spoken style, or reply SKIP if the context is insufficient."
    )

    pending = ""      # confirmed answer text not yet flushed as a whole sentence
    decided = False   # whether we've ruled out the SKIP sentinel
    spoke_any = False
    _first_token_ms = None
    for delta in llm.stream_complete(
        system, user, max_tokens=400, model=settings.brain_model_fast
    ):
        if _first_token_ms is None:
            _first_token_ms = (time.perf_counter() - _t0) * 1000
        pending += delta
        if not decided:
            head = pending.lstrip()
            # Wait until we have enough characters to distinguish SKIP from a real
            # answer that merely starts with those letters (e.g. "Skipping ...").
            if len(head) < 5 and head.upper() != "SKIP":
                continue
            if _is_skip(head):
                return  # insufficient context — stay silent
            decided = True

        pending, sentences = _split_sentences(pending)
        for s in sentences:
            if not spoke_any:
                _first_sentence_ms = (time.perf_counter() - _t0) * 1000
                print(
                    f"[latency] answer_stream retrieve={_retrieve_ms:.0f}ms "
                    f"first_token={_first_token_ms:.0f}ms "
                    f"first_sentence={_first_sentence_ms:.0f}ms",
                    flush=True,
                )
            yield s
            spoke_any = True

    tail = pending.strip()
    if not decided:
        # Very short answer that never crossed the decision threshold.
        if tail and not _is_skip(tail):
            yield tail
            spoke_any = True
    elif tail:
        yield tail
        spoke_any = True

    if spoke_any and citation:
        yield f"— per {citation}"


def _split_sentences(buf: str) -> tuple[str, list[str]]:
    """Pull all complete sentences out of `buf`; return (remainder, sentences)."""
    sentences: list[str] = []
    while True:
        m = _SENTENCE.search(buf)
        if not m:
            break
        cut = m.end()
        s = buf[:cut].strip()
        buf = buf[cut:]
        if s:
            sentences.append(s)
    return buf, sentences


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


PROACTIVE_SYSTEM = """{persona}

You are silently observing a live work meeting that is wrapping up. Using ONLY \
the company process documents provided, decide whether ONE important process step \
is clearly missing or at risk (a missing owner, approval, deadline, required \
document, or unresolved blocker) that the team has NOT addressed.

Be conservative: only speak if you are genuinely confident it is both important \
and unaddressed. Silence is the default. If in doubt, do not speak.

Return ONLY a JSON object:
{{
  "should_speak": <true|false>,
  "line": "<one short spoken sentence flagging it, phrased politely as a question>",
  "gap_type": "<owner|approval|deadline|document|blocker>",
  "citations": ["<source filename>"],
  "confidence": <0.0-1.0>
}}"""


def proactive_flag(avatar: Avatar, transcript_text: str, *, k: int = 6) -> dict:
    """Decide if the avatar should proactively flag ONE missing step. Default: no."""
    chunks = retrieve(
        avatar, transcript_text[-2000:] or "process owners approvals deadlines", k=k
    )
    if _is_stub():
        return _stub_proactive(chunks, transcript_text)

    raw = llm.complete(
        PROACTIVE_SYSTEM.format(persona=avatar.persona_prompt),
        (
            f"Company process context:\n\n{_format_context(chunks)}\n\n"
            f"Meeting so far:\n\n{transcript_text}\n\n"
            "Respond with the JSON object only."
        ),
        max_tokens=300,
        model=settings.brain_model_fast,  # latency-critical: fast model
    )
    r = _parse_json(raw)
    r.setdefault("should_speak", False)
    r.setdefault("confidence", 0.0)
    r.setdefault("line", "")
    return r


def _stub_proactive(chunks: list[Retrieved], transcript_text: str) -> dict:
    """Offline heuristic: flag a missing owner/approval if the docs mention one
    and the transcript doesn't clearly assign it."""
    low = transcript_text.lower()
    ctx = " ".join(c.text.lower() for c in chunks)
    if "approv" in ctx and "approv" in low and "security lead" not in low:
        return {
            "should_speak": True,
            "line": "Before we close, I didn't hear who's giving the required approval — should we assign an owner for that?",
            "gap_type": "approval",
            "citations": [chunks[0].source] if chunks else [],
            "confidence": 0.72,
        }
    return {"should_speak": False, "line": "", "confidence": 0.0}


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
