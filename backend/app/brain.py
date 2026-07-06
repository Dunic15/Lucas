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

from . import llm, meeting_state, tools
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


def post_provider() -> str:
    """Provider for the NON-realtime post-meeting path.

    BRAIN_PROVIDER_POST lets the artifact use a quality model while the live
    path stays on the fast provider. Falls back to the live provider when
    unset, and to the stub when anthropic is chosen without a key.
    """
    p = (settings.brain_provider_post or settings.brain_provider).lower()
    if p == "anthropic" and not settings.anthropic_api_key:
        return "stub"
    return p


# ─────────────────────────── live answers ───────────────────────────
ANSWER_SYSTEM = """{persona}

You are a callable AI process expert that has been invited into a live work \
meeting. You speak ONLY from the company process documents provided as context. \
You are concise: this is spoken aloud, so answer in 1-3 short sentences a person \
can absorb by ear.

Rules:
- Use ONLY the provided context. Do not invent steps, owners, or approvals.
- If the context only partly answers the question, give the supported part first,
  then say what is missing. Do not refuse a useful partial answer.
- If the context does not contain the answer, say so plainly and ask for the
  smallest missing detail. Do not guess.
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
# the rest. The confidence JSON can't stream, so the model may use a SKIP
# sentinel only when the speech is not addressed to Laura. Missing context should
# produce a useful partial answer or a brief "I don't have that" response.
ANSWER_STREAM_SYSTEM = """{persona}

You are Laura, a warm, sharp AI assistant participating in a live spoken \
conversation. You are a capable general assistant FIRST — think ChatGPT or \
Claude in a meeting: direct, concrete, genuinely useful — and a company/fund \
expert only when the question touches the provided documents. Default to 1-2 \
punchy sentences (3 max); never restate the question, never open with filler \
like "great question". Plain text only — no markdown, bullets, headings, \
JSON, or preamble. NEVER mention documents, context, knowledge bases, or what \
you do or don't "have access to" unless you are actually citing a company \
document in this answer.

How to respond:
- General questions (world knowledge, advice, explanations, opinions, news, \
math, small talk, jokes): answer directly and naturally from your own \
knowledge. Do NOT mention documents, context, or what you were given. Never \
refuse just because it isn't in the documents.
- Questions about the company's processes, the SFF fund, or its portfolio: \
ground your answer in the provided context and name the source doc briefly \
and naturally (e.g. "per the onboarding SOP"). Don't invent specific steps, \
owners, or approvals that aren't there; if the context only partly covers \
it, give the useful part and say what you'd check.
- If you were given web search results or used search, answer from them and \
mention it's from a quick search.
- Live transcripts are noisy — infer the likely intent and answer what the \
person most likely meant.
- Reply with the single word SKIP (and nothing else) ONLY when the speech is \
clearly NOT directed at you — e.g. two other people talking to each other. \
When someone seems to be addressing you or asking anything at all, respond. \
When in doubt, respond."""


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


# Questions that want FRESH information from the internet — routed to the
# compound model with built-in server-side web search (Groq only).
_SEARCH_INTENT = re.compile(
    r"\b(search|look up|google|on the internet|online|web|latest|news|"
    r"today|tonight|yesterday|currently|right now|this (week|month|year)|"
    r"price of|stock|weather|score|who won|happened|202[5-9])\b",
    re.IGNORECASE,
)


def _live_model(question: str) -> str:
    """Model for one live answer: the fast default, or the web-search-capable
    compound model when the question asks for fresh information."""
    if (
        settings.live_search_enabled
        and settings.groq_api_key  # compound runs on Groq regardless of live provider
        and _SEARCH_INTENT.search(question or "")
    ):
        return settings.live_search_model
    return settings.brain_model_fast


def answer_question_stream(
    avatar: Avatar, question: str, *, history: str = "", memory: str = "", k: int = 6
):
    """Yield spoken sentences as they are generated. Yields nothing (stays silent)
    only when the model judges the speech was not addressed to Laura (SKIP).

    `memory` is the cross-meeting carryover brief (ledger.carryover_brief):
    what previous sessions of this same meeting left open or decided. Empty
    for first-time meetings — the prompt then carries no memory block at all.
    """
    _t0 = time.perf_counter()
    chunks = retrieve(avatar, _retrieval_query(question, history), k=k)
    _retrieve_ms = (time.perf_counter() - _t0) * 1000
    # Only ground in the docs when they actually match the question —
    # irrelevant chunks bias the model into doc-quoting general answers.
    if chunks and chunks[0].score < settings.rag_min_context_score:
        chunks = []
    citation = chunks[0].source if chunks else ""

    if _is_stub():
        r = _stub_answer(chunks)
        if r.get("sufficient_context"):
            yield r["answer"]
            if citation:
                yield f"— per {citation}"
        return

    convo = f"Recent meeting conversation:\n{history}\n\n" if history.strip() else ""
    remembered = (
        f"What Laura remembers from previous meetings of this series:\n{memory}\n\n"
        if memory.strip()
        else ""
    )
    context_block = (
        f"Company/fund document context (relevant to this question):\n\n{_format_context(chunks)}\n\n"
        if chunks
        else ""
    )
    system = ANSWER_STREAM_SYSTEM.format(persona=avatar.persona_prompt)
    user = (
        f"{context_block}"
        f"{remembered}"
        f"{convo}"
        f"Someone in the meeting asked:\n{question}\n\n"
        "Answer in spoken style. Reply SKIP only if this was clearly not directed at Laura."
    )

    pending = ""      # confirmed answer text not yet flushed as a whole sentence
    decided = False   # whether we've ruled out the SKIP sentinel
    spoke_any = False
    _first_token_ms = None
    _model = _live_model(question)
    if _model == settings.live_search_model:
        # Web-search answers go NON-streamed: compound's streaming reliably
        # returns reasoning/tool deltas but (verified live, repeatedly) often
        # ends without any content, while non-streaming completes every time.
        # Search asks are rare; a dependable ~4s answer beats a broken stream.
        try:
            raw = llm.complete(
                "You answer in 1-3 short spoken sentences, no markdown. Use web "
                "search for current information and mention it's from a quick search.",
                f"{convo}Use web search, then answer briefly:\n{question}",
                max_tokens=2048,
                model=_model,
                provider="groq",  # compound lives on Groq even when live brain is Claude
            )
        except Exception:
            raw = ""
        head = (raw or "").strip()
        _search_failed = (not head) or re.search(
            r"(not able to browse|can'?t browse|cannot browse|"
            r"don'?t have (live|real-?time|internet|web) access)",
            head,
            re.IGNORECASE,
        )
        if head and not _is_skip(head) and not _search_failed:
            buf, sentences = _split_sentences(head + " ")
            for sent in sentences:
                yield sent
            if buf.strip():
                yield buf.strip()
            return
        # Search flaked (compound is beta-grade: sometimes refuses or returns
        # nothing) — fall THROUGH to the fast model so she still answers from
        # her own knowledge instead of going silent.
        question = f"{question} (You could not search the web just now — answer from your knowledge and say it may not be current.)"
    _max_tokens = 400
    for delta in llm.stream_complete(
        system, user, max_tokens=_max_tokens, model=_model
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

    # Citation is not auto-appended: it made small talk read absurdly ("nice joke
    # — per onboarding_sop.md"). The model is instructed to name the source doc
    # itself when (and only when) it actually answers from a process document.


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


# ───────────────────── live answers WITH tools (the 'act' layer) ─────────
# Same grounding as answer_question, but the model can CALL tools to do things:
# calculate, reason about a deadline, or look up a record. Not streamed — tool
# use needs a round-trip first — so this is for the direct web avatar / demo,
# not (yet) the latency-critical meeting path. Falls back to a plain grounded
# answer when tools aren't available (stub/offline), so nothing breaks.
ANSWER_TOOLS_SYSTEM = """{persona}

You are Laura, a warm, helpful AI assistant in a live spoken conversation. Keep \
replies to 1-3 short sentences a person can absorb by ear. Plain text only — no \
markdown, bullets, headings, or preamble.

You can USE TOOLS to do things, not just recall from documents:
- calculator — for ANY arithmetic (percentages, totals, per-seat cost, annualizing).
- date_math — today's date, or how many days until a deadline/renewal.
- lookup_record — check a customer account (plan, seats, MRR, renewal, owner).

Call a tool whenever it makes your answer more concrete or accurate; you may chain \
them (e.g. look up a renewal date, then compute the days until it). Ground company \
process facts in the provided context and name the source doc when you use one. When \
you calculated or looked something up, state the concrete result plainly."""


def answer_with_tools(
    avatar: Avatar, question: str, *, history: str = "", k: int = 6
) -> dict:
    """Grounded answer that may CALL tools to act. Returns answer + tools_used."""
    chunks = retrieve(avatar, _retrieval_query(question, history), k=k)

    if _is_stub():
        # No tool use offline — fall back to the deterministic grounded answer.
        r = _stub_answer(chunks)
        return {"answer": r["answer"], "tools_used": [], "citations": r.get("citations", [])}

    convo = f"Recent meeting conversation:\n{history}\n\n" if history.strip() else ""
    system = ANSWER_TOOLS_SYSTEM.format(persona=avatar.persona_prompt)
    user = (
        f"Company process context:\n\n{_format_context(chunks)}\n\n"
        f"{convo}"
        f"Someone asked:\n{question}\n\n"
        "Use tools if they'd help, then answer in spoken style."
    )
    text, used = llm.complete_with_tools(
        system, user, tools.TOOL_SPECS, tools.dispatch, model=settings.brain_model_fast
    )
    if used:
        print(f"[tools] {question[:60]!r} -> " + ", ".join(u["tool"] for u in used), flush=True)
    return {
        "answer": (text or "").strip(),
        "tools_used": used,
        "citations": [chunks[0].source] if chunks else [],
    }


# ───────────────────────── post-meeting ─────────────────────────────
POSTMEETING_SYSTEM = """You analyze a meeting transcript against company \
process knowledge. Produce a crisp post-meeting artifact a team can act on.

Detect PROCESS GAPS, specifically any of: missing owner, missing deadline, \
missing approval, missing required document, unresolved blocker. Only flag a \
gap if it is genuinely implied by the discussion; do not pad the list.

Return ONLY a JSON object:
{
  "summary": "<3-5 sentence plain summary of what was discussed and decided>",
  "decisions": ["<each decision the group actually reached, one short line>"],
  "actions": [
    {"item": "<action>", "owner": "<name or 'UNASSIGNED'>", "deadline": "<stated deadline or ''>", "gap_type": "<owner|deadline|approval|document|blocker|none>"}
  ],
  "risks": ["<each risk or unresolved blocker raised, one short line>"],
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


def proactive_flag(
    avatar: Avatar,
    transcript_text: str,
    *,
    state: "meeting_state.MeetingState | None" = None,
    memory: str = "",
    k: int = 6,
) -> dict:
    """Decide if the avatar should proactively flag ONE missing step. Default: no.

    When the tracked MeetingState says a critical required process step never
    happened, this is deterministic — the templated intervention line goes out
    with no model call (reliable in stub AND Claude mode, zero extra latency).
    Otherwise the model judges from the transcript, with the structured state
    as extra grounding.
    """
    if state is not None and state.missing_critical():
        return {
            "should_speak": True,
            "line": meeting_state.intervention_line(state),
            "gap_type": "process_step",
            "citations": [],
            "missing_steps": state.missing_critical(),
            "confidence": 0.95,
        }

    chunks = retrieve(
        avatar, transcript_text[-2000:] or "process owners approvals deadlines", k=k
    )
    if _is_stub():
        return _stub_proactive(chunks, transcript_text)

    state_block = (
        f"Structured meeting state (tracked silently):\n{meeting_state.state_summary(state)}\n\n"
        if state is not None
        else ""
    )
    memory_block = (
        f"Carried over from previous meetings of this series (may still be unaddressed):\n{memory}\n\n"
        if memory.strip()
        else ""
    )
    raw = llm.complete(
        PROACTIVE_SYSTEM.format(persona=avatar.persona_prompt),
        (
            f"Company process context:\n\n{_format_context(chunks)}\n\n"
            f"{state_block}"
            f"{memory_block}"
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
    """Full post-meeting artifact: summary, decisions, actions, missing process
    steps, readiness score, risks, and a draft follow-up email.

    The tracked MeetingState (rebuilt from the transcript) supplies the
    deterministic parts — missing_steps and readiness_score come from the
    process template, not model judgement — and backfills decisions/risks when
    the model returns none.
    """
    state = meeting_state.build_from_text(avatar, transcript_text)

    if post_provider() == "stub":
        artifact = _stub_post_meeting(avatar, transcript_text, state)
    else:
        # Ground gap-detection in the actual process docs.
        chunks = retrieve(
            avatar, transcript_text[-3000:] or "process steps owners approvals", k=k
        )
        raw = llm.complete(
            POSTMEETING_SYSTEM,
            (
                f"Relevant company process context:\n\n{_format_context(chunks)}\n\n"
                f"Structured meeting state (tracked during the meeting):\n"
                f"{meeting_state.state_summary(state)}\n\n"
                f"Meeting transcript:\n\n{transcript_text}\n\n"
                "Respond with the JSON object only."
            ),
            max_tokens=1200,
            provider=post_provider(),
        )
        artifact = _parse_json(raw)

    return _finish_artifact(artifact, state)


def _finish_artifact(artifact: dict, state: "meeting_state.MeetingState") -> dict:
    """Normalize to the full artifact schema; state fills the deterministic
    fields and backfills anything the model left out."""
    artifact.setdefault("summary", "")
    artifact.setdefault("follow_up_email", {})
    if not artifact.get("decisions"):
        artifact["decisions"] = [d["decision"] for d in state.decisions]
    if not artifact.get("risks"):
        artifact["risks"] = [r["risk"] for r in state.risks]
    # Old consumers (demo page, Slack formatter) read "checklist"; new schema
    # calls it "actions". Keep both pointing at the same list.
    actions = artifact.get("actions") or artifact.get("checklist") or []
    artifact["actions"] = actions
    artifact["checklist"] = actions
    artifact["missing_steps"] = list(state.missing_steps)
    artifact["readiness_score"] = state.readiness_score()
    artifact["meeting_type"] = state.meeting_type
    return artifact


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


def _stub_post_meeting(
    avatar: Avatar, transcript_text: str, state: "meeting_state.MeetingState"
) -> dict:
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
    if state.meeting_type:
        summary += (
            f" Detected a {state.meeting_type.replace('_', ' ')} meeting: "
            f"{len(state.completed_steps)}/{len(state.required_steps)} required "
            "process steps covered."
        )
    return {
        "summary": summary,
        "decisions": [d["decision"] for d in state.decisions],
        "risks": [r["risk"] for r in state.risks],
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
