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
import random
import re
import time

from . import llm, meeting_state, tools
from .avatars import Avatar
from .config import settings
from .rag import retrieve, retrieve_about, Retrieved

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


def _mission_directive(mission: str) -> str:
    """A standing per-meeting MISSION folded into the system prompt as an extra
    instruction. Empty mission -> "" (the prompt is byte-identical to today).

    The avatar keeps the objective in mind and RESURFACES it if left unmet — but
    only at a natural opening. This is an INSTRUCTION, never a gate: the existing
    turn-taking / hand-raise rules still decide WHEN she may speak, so she never
    barges in to force the mission."""
    m = (mission or "").strip()
    if not m:
        return ""
    return (
        "\n\nMISSION FOR THIS MEETING (set by the admin): "
        + m
        + " Keep this objective in mind throughout. If the meeting is nearing its "
        "end and the objective is still unaddressed, raise it ONCE at a natural "
        "opening — briefly and politely, phrased as a question. Never interrupt, "
        "never force it, and let it go if no natural moment comes."
    )


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
    avatar: Avatar, question: str, *, history: str = "", k: int = 4,
    org_id: str = "",
) -> dict:
    """Retrieve + answer for one avatar. Returns answer/citations/confidence.

    `history` is the recent meeting conversation (last few "Speaker: line" turns)
    so the avatar understands *this* discussion, not just the isolated question.
    `org_id` scopes retrieval to include that org's private ingested docs
    (rag.retrieve) — "" keeps the shared base pack only.
    """
    chunks = _retrieve_for(avatar, question, history, k, org_id=org_id)

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
    # Grounding floor: the model self-reports sufficient_context, but on weak
    # retrieval it sometimes labels a world-knowledge answer as document-grounded
    # (sufficient_context=true + doc citations) — a false "this came from your
    # docs" signal. If the best chunk is below the relevance floor, the answer is
    # NOT grounded in company context: correct the metadata (the answer text stays
    # — Laura is a general assistant first, so world-knowledge answers are fine,
    # just honestly labelled un-grounded). Grounded matches score ~0.6-0.75;
    # irrelevant ones ~0.30.
    if max((c.score for c in chunks), default=0.0) < settings.answer_grounding_floor:
        result["sufficient_context"] = False
        result["citations"] = []
    return result


# ─────────────────── live answers (streamed) ────────────────────────
# Same trust contract as answer_question, but streamed for low latency: the
# avatar starts speaking the first sentence while the model is still generating
# the rest. The confidence JSON can't stream, so the model may use a SKIP
# sentinel only when the speech is not addressed to Laura. Missing context should
# produce a useful partial answer or a brief "I don't have that" response.
ANSWER_STREAM_SYSTEM = """{persona}

You are {name}, a warm, sharp AI assistant participating in a live spoken \
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
- Questions about the company's processes or anything covered by the provided \
documents: ground your answer in that context and name the source doc briefly \
and naturally (e.g. "per the onboarding SOP"). Don't invent specific steps, \
owners, or approvals that aren't there; if the context only partly covers \
it, give the useful part and say what you'd check.
- If you were given web search results or used search, answer from them and \
mention it's from a quick search.
- Live transcripts are noisy — infer the likely intent and answer what the \
person most likely meant.
- Reply in the language the person spoke to you in — an Italian question gets \
an Italian answer. Follow the conversation if it switches language.
- Meetings often have several people. When a roster and the speaker's name are \
provided, use them: you KNOW who and how many are in the room, so answer \
"who's here / how many are we?" directly from the roster. Address the person \
who asked by name when it flows naturally (not every single line), and never \
attribute a statement to the wrong person — the "Speaker: line" transcript \
tells you who said what.
- Contribute something NEW. Never repeat or rephrase what a participant \
already said as if it were your own point — if you have nothing to add \
beyond what was just said, reply SKIP.
- When someone asks you to DO something (send an email, book or schedule a \
meeting, create a task), say you'll take care of it right after the call — \
never that it's already done. If a required detail is missing — the \
recipient's email address to send to, or a concrete date and time to book — \
ASK for it in the same reply so it can be captured; never invent an email \
address or a time.
- Reply with the single word SKIP (and nothing else) when the speech is \
clearly NOT directed at you: two other people talking to each other, or a \
line addressed to ANOTHER participant by name ("Marco, can you take this?"). \
In a 1:1 conversation, when in doubt, respond. With several people in the \
room, only respond when you're addressed, asked, or the question is clearly \
open to the room."""


def _is_skip(head: str) -> bool:
    """True if `head` is a standalone SKIP sentinel (not a word like 'Skipping')."""
    return head[:4].upper() == "SKIP" and (len(head) == 4 or not head[4].isalpha())


# First streamed chunk can be shorter than min_chars: the opening words are the
# perceived latency, and a slightly clipped first breath beats half a second
# more of silence. Later chunks keep the caller's min_chars for prosody.
_FIRST_CHUNK_MIN_CHARS = 24


def _state_has_signal(state: "meeting_state.MeetingState") -> bool:
    """True when the tracker actually captured something worth prompting with."""
    return bool(
        state.required_steps
        or state.decisions
        or state.owners
        or state.deadlines
        or state.risks
        or state.open_questions
        or any(
            p["commitments"] or p["questions"] or p["risks"]
            for p in state.per_person.values()
        )
    )


def _roster_block(
    avatar: Avatar,
    roster: "list[str] | None",
    state: "meeting_state.MeetingState | None",
) -> str:
    """One compact prompt line: who is in the room, and who hasn't spoken yet.

    Quiet detection compares roster names with the per-person tracker (first
    names, so 'Marco' from diarization matches 'Marco Rossi' from the roster).
    Lets her answer "who's here / who hasn't spoken?" and address the room
    accurately — at the cost of one short line, latency-neutral.
    """
    if not roster:
        return ""
    block = (
        f"In the meeting right now, besides {avatar.name}: "
        f"{', '.join(roster)} ({len(roster)} "
        f"{'person' if len(roster) == 1 else 'people'})."
    )
    if state is not None and len(roster) > 1:
        # Preserve multiplicity: if two humans are both named Alex and only
        # one has spoken, the other must still count as quiet. Names are for
        # presentation only; identity remains participant-id based in state.
        spoke = [
            str(p.get("name") or key).split()[0].lower()
            for key, p in state.per_person.items()
        ]
        quiet = []
        for name in roster:
            first = name.split()[0].lower()
            if first in spoke:
                spoke.remove(first)
            else:
                quiet.append(name)
        if quiet:
            block += f" Not yet heard from: {', '.join(quiet)}."
    return block + "\n\n"


def _retrieval_query(question: str, history: str = "") -> str:
    """Retrieve against the ask plus recent context so vague live speech works."""
    question = (question or "").strip()
    history = (history or "").strip()
    if not history:
        return question
    return f"{history[-1200:]}\n\nCurrent ask: {question}"


# Self-questions — someone asking about the AVATAR herself ("how do you
# work?", "chi sei?"). These ground in the about/ meta docs, which are kept
# OUT of process retrieval (a real "what's missing for go-live?" must never
# pull Laura's own playbook). Deliberately specific: generic words like
# "your cost" alone don't match, or project questions would misroute.
_ABOUT_INTENT = re.compile(
    r"\b(how (do|does) (you|laura) work|what (can|do) you (do|know)\b|"
    r"who (are|built|made|created) you\b|what are you\b|"
    r"are you (an? )?(ai|bot|robot|human|real)\b|"
    r"(your|laura'?s) (architecture|brain|stack|pipeline|tech stack|web brows\w+|brows\w+)\b|"
    r"how (were|are) you (built|made|designed|trained)\b|"
    r"(you|laura) (built|made|powered|based) (on|with|by)\b|"
    r"what (model|llm|models)\b.{0,24}\b(you|use|using|run)|"
    # Capability questions about web browsing ("CAN you browse the web?")
    # are self-questions; bare tasks ("search the web for X") are not —
    # the modal + you is required so task asks keep normal routing.
    r"((can|could|do|will) (you|laura)|are (you|laura) able to)\b.{0,24}\b(browse|search|surf|navigate|look\w*)\b.{0,20}\b(web|internet|online|browser|websites?)\b|"
    r"(puoi|sai|riesci a?)\b.{0,20}\b(navigar\w+|cercar\w+|browsar\w+)\b.{0,20}\b(web|internet|online|sit[oi])\b|"
    r"come funzioni\b|come sei fatt\w+|cosa (sai|puoi) fare|"
    r"che modell[oi]\b|su che (modello|tecnologia)|con che (modello|tecnologia)|"
    r"chi (sei|ti ha creat\w+|ti ha fatt\w+)|sei (un[ao]? )?(ai|robot|bot|uman\w+))\b",
    re.IGNORECASE,
)


def _is_about_avatar(question: str) -> bool:
    return bool(_ABOUT_INTENT.search(question or ""))


def _retrieve_for(
    avatar: Avatar, question: str, history: str, k: int, *, org_id: str = ""
) -> list[Retrieved]:
    """Route retrieval: self-questions hit the about/ pack, everything else the
    real knowledge docs (plus the org's private index when org_id is given).
    Self-questions retrieve on the bare ask (they're direct), process questions
    keep the history-augmented query."""
    if _is_about_avatar(question):
        return retrieve_about(avatar, question, k=k)
    query = _retrieval_query(question, history)
    if org_id:
        return retrieve(avatar, query, k=k, org_id=org_id)
    # No org scope → the pre-seam call shape, so tests/instrumentation that
    # wrap retrieve() with the old signature keep working unchanged.
    return retrieve(avatar, query, k=k)


# Cheap language sniff for a live utterance: enough Italian function words →
# treat the turn as Italian (drives announce/ack/filler language — the ANSWER
# language is handled by the model itself via the prompt).
_IT_HINT = re.compile(
    r"\b(che|chi|come|cosa|cos'è|quanto|quando|perch[eé]|dove|sono|sei|siamo|"
    r"questo|questa|quali|della|delle|degli|nella|sulla|puoi|potresti|"
    r"dovremmo|anche|però|già|più|grazie|ciao|allora|cerca|dimmi|fammi|"
    r"oggi|ieri|domani|notizie|ultime|adesso|ancora|sempre|qualcosa|tutto|"
    r"fare|dire|dicono|vorrei|serve|abbiamo|avete|possiamo|riunione|settimana|"
    r"[a-z]+zione|[a-z]+mente)\b|[àèéìòù]",
    re.IGNORECASE,
)


def sounds_italian(text: str) -> bool:
    """True when the utterance reads as Italian (2+ Italian word/accent hits)."""
    return len(_IT_HINT.findall(text or "")) >= 2


# Questions that want FRESH information from the internet — routed to Claude's
# native web_search tool (llm.web_search on live_search_model). Provider-neutral:
# it never depends on the fast provider. English + Italian triggers: Laura's
# meetings are bilingual, and an intent regex that only speaks English silently
# disables the feature (and its spoken announce) for Italian speakers.
_SEARCH_INTENT = re.compile(
    r"\b(search|look up|google|on the internet|online|web|latest|news|"
    r"today|tonight|yesterday|currently|right now|this (week|month|year)|"
    r"price of|stock|weather|score|who won|happened|202[5-9]|"
    # Italian
    r"cerca\w*|cercami|su internet|ultime notizie|notizie|oggi|stasera|ieri|"
    r"attualmente|in questo momento|questa settimana|questo mese|quest.anno|"
    r"prezzo di|quanto costa|meteo|che tempo fa|chi ha vinto|successo ieri|"
    # SFF/fund questions now come from the web too (no local pack) — see persona.
    r"sff|swiss founders fund|founders fund|portfolio)\b",
    re.IGNORECASE,
)


def _wants_search(question: str) -> bool:
    """True if this asks for fresh/current info we should look up on the web.
    Web search now runs on Claude (Anthropic's native web_search tool), so it's
    gated on the Anthropic key, not Groq."""
    return bool(
        settings.live_search_enabled
        and settings.anthropic_api_key
        and _SEARCH_INTENT.search(question or "")
    )


def _live_model(question: str) -> str:
    """The model for one live answer (web-search model for fresh-info asks)."""
    return settings.live_search_model if _wants_search(question) else settings.brain_model_fast


def wants_web_search(question: str) -> bool:
    return _wants_search(question)


# Clearly-analytical asks — worth the more reliable/capable Claude model even on
# the live path (Groq llama is weakest exactly here, and rate-limits under load).
_COMPLEX_INTENT = re.compile(
    r"\b(analy[sz]e|analysis|compare|comparison|versus|trade[- ]?offs?|"
    r"pros and cons|strateg|evaluate|assess|recommend|draft|write (a|an|me|up)|"
    r"step[- ]by[- ]step|in detail|break (it|this) down|walk me through|"
    r"should (i|we|they)|explain why|reason through|think through|"
    # Italian
    r"analizza\w*|confronta\w*|paragona\w*|valuta\w*|consiglia\w*|consiglieresti|"
    r"raccomand\w+|scrivi(mi)?|redigi|spiega(mi)? perch[eé]|ragiona\w*|"
    r"passo (per|dopo) passo|nel dettaglio|dovremmo|conviene|pro e contro)\b",
    re.IGNORECASE,
)


def wants_deep_thought(question: str) -> bool:
    """True when _live_route will pick the slower 'complex' Claude path —
    callers can announce the pause ('let me think') before the answer starts."""
    return bool(settings.anthropic_api_key and _COMPLEX_INTENT.search(question or ""))


# Direct asks for the avatar to DO something asynchronous ("can you send the
# recap…", "please book a follow-up") — main.py captures these DETERMINISTICALLY
# on the live path (no LLM, no tool loop) and promises follow-up after the call.
# Deliberately NARROW: only verbs that unambiguously request an act performed
# AFTER the meeting. Content-query verbs (check/verify/look/see/find out,
# controllare/verificare/guardare/cercare) are EXCLUDED on purpose — "can you
# check if X" is a question the streamed path answers live, and hijacking it
# would trade away streaming latency (the forbidden trade) AND answer wrongly.
# "remind" matches only the "remind me/us to …" form ("remind me what we
# decided" is a memory question). A missed match still reaches the artifact via
# the post-meeting summarizer; a false positive wrongly promises a follow-up.
_ACTION_VERBS = (
    r"(?:send|schedule|book|set\s+up|draft|prepare|email|invite|"
    # Messaging/posting verbs. `post(?!-)` so "post-meeting" (a common phrase)
    # is NOT read as an imperative "post"; "post to Slack" / "post the recap" are.
    r"ping|dm|message|post(?!-)|"
    r"follow\s+up|organi[sz]e|arrange|"
    r"remind\s+(?:me|us|him|her|them)\s+to|"
    r"(?:create|open)\s+(?:a\s+|an\s+|the\s+)?(?:ticket|task|issue|doc(?:ument)?|event|meeting|invite)|"
    r"add\s+(?:\w+\s+)?to\s+(?:the\s+|my\s+|our\s+)?(?:calendar|slack|notion|channel))"
)
# Optional leading fillers (EN + IT) so "Ok, schedule…", "So send…", "Allora
# manda…" still read as bare imperatives (real speech rarely starts clean on the
# verb).
_ACTION_LEAD = (
    r"(?:(?:ok(?:ay)?|so|and|then|also|now|alright|yeah|hey|please|"
    r"allora|quindi|dai|poi)[,\s]+)*"
)
# Italian imperative stems (the bare "manda…/prenota…" command form). Kept in
# sync with the periphrastic Italian branch below; content-query verbs
# (controlla/verifica/guarda/cerca) are deliberately EXCLUDED, like the English
# side, so "controlla se…" stays a live question.
_ACTION_VERBS_IT = (
    r"(?:manda(?:mi)?|invia(?:mi)?|inoltra|spedisci|prenota|fissa|"
    # "schedula" (italianized English) is how calendar asks actually sound live
    # (2026-07-10 test: "schedula il meeting" routed to the slow answer path
    # instead of instant capture), plus the "crea/aggiungi" calendar shapes.
    r"organi[sz]za|programma|schedula(?:mi)?|prepara|ricordami\s+di|"
    r"crea\s+(?:un[oa]?\s+|il\s+|la\s+)?(?:meeting|riunione|evento|invito|task|ticket)|"
    r"aggiungi\s+(?:[\w']+\s+){0,4}al\s+calendario|metti\s+in\s+calendario)"
)
_ACTION_INTENT = re.compile(
    rf"\b(?:can|could|will|would)\s+you\s+(?:please\s+)?{_ACTION_VERBS}\b"
    rf"|\bplease\s+{_ACTION_VERBS}\b"
    # Bare imperative: the verb leads the (wake-stripped) ask, e.g. "schedule a
    # follow-up with Marco", "send Priya an email", "post to Slack". The ^ anchor
    # is the false-positive guard — plain statements ("we should send X", "I'll
    # email him") don't START with the verb.
    rf"|^{_ACTION_LEAD}{_ACTION_VERBS}\b"
    # Italian bare imperative: "manda una mail…", "prenota una call…" — same ^
    # anchor so mid-sentence indicatives ("dovremmo mandare…") stay out.
    rf"|^{_ACTION_LEAD}{_ACTION_VERBS_IT}\b"
    # Italian periphrastic: "puoi/potresti mandare…", "mi mandi/prenoti…", "ricordami di…"
    r"|\b(?:puoi|potresti|riesci\s+a)\s+(?:mandar|inviar|prenotar|fissar|"
    r"organizzar|preparar|schedular|programmar|crear)\w*\b"
    r"|\b(?:puoi|potresti)\s+ricordar(?:mi|ci)\s+di\b"
    r"|\bmi\s+(?:mandi|invii|prenoti|fissi|prepari|scheduli|programmi)\b"
    r"|\bricorda(?:mi|ci)\s+di\b",
    re.IGNORECASE,
)


def wants_action_capture(question: str) -> bool:
    """True when the utterance directly asks the avatar to DO something after
    the call — main.py's live loop captures it (queue_action seam) and speaks
    a fixed confirmation instead of routing the turn to an answer path."""
    return bool(_ACTION_INTENT.search(question or ""))


def _live_route(question: str) -> tuple[str, str]:
    """(provider, model) for one live answer:
      - web search (fresh info)       -> Claude + native web_search tool (the
                                         "search" pseudo-provider routes to it)
      - clearly-complex reasoning     -> Claude (brain_model_complex): reliable +
                                         capable, and it dodges Groq's rate limits
      - everything else (chat/simple) -> the fast default provider (Groq llama)
    """
    if _wants_search(question):
        return "search", settings.live_search_model
    if wants_deep_thought(question):
        return "anthropic", settings.brain_model_complex
    return settings.brain_provider, settings.brain_model_fast


# Spoken BEFORE the (slow) web-search call: a few seconds of silence reads as a
# bug, an announced lookup reads as diligence. Two language pools, picked by the
# question's language; 5+ variants each so the repeat guard (120s window) never
# silently swallows the announce during back-to-back searches. Safe to say
# unconditionally: the search path never SKIPs, so an answer always follows.
_SEARCH_ANNOUNCE_EN = (
    "One moment — let me look that up online.",
    "Give me a second, I'll check the latest on that.",
    "Let me search for that quickly.",
    "Hang on, checking the web for you.",
    "Let me pull that up — one sec.",
)
_SEARCH_ANNOUNCE_IT = (
    "Un attimo — lo cerco online.",
    "Dammi un secondo, controllo le ultime su questo.",
    "Vado a cercarlo, un momento.",
    "Aspetta, guardo sul web.",
    "Un secondo che controllo.",
)
# Every announce line, for the boot-time TTS prewarm.
SEARCH_ANNOUNCE_LINES = _SEARCH_ANNOUNCE_EN + _SEARCH_ANNOUNCE_IT


_SEARCH_FAIL_RE = re.compile(
    r"(not able to browse|can'?t browse|cannot browse|"
    r"don'?t have (live|real-?time|internet|web) access)",
    re.IGNORECASE,
)


def _web_search_answer(question: str, convo: str = "") -> str:
    """One web-search answer via Claude's native web_search tool (live_search_model,
    default Sonnet — strong at search + dynamic result filtering).

    Returns the spoken answer text, or "" if search errored, refused, or returned
    nothing — so the caller can fall back to normal reasoning instead of going
    silent. Shared by the meeting path and the interactive /live/act path.
    """
    try:
        raw = llm.web_search(
            "You answer in 1-3 short spoken sentences, no markdown. Use web search "
            "for current information and mention it's from a quick search.",
            f"{convo}Use web search, then answer briefly:\n{question}",
            model=settings.live_search_model,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[search] web_search failed: {e}", flush=True)
        return ""
    head = (raw or "").strip()
    if not head or _is_skip(head) or _SEARCH_FAIL_RE.search(head):
        return ""
    return head


# ── honest caveat on ungrounded PROCESS answers (settings.caveat_ungrounded_
#    process_answers) ──
# On thin retrieval the chunks are dropped (below rag_min_context_score) and she
# answers from world knowledge — fine for a general question, but for a COMPANY/
# PROCESS-specific one ("what's OUR refund policy?") an authoritative world-
# knowledge answer reads as if it came from their docs, undercutting the
# "grounded + cited from YOUR process docs" pitch. This lightweight lexical
# heuristic flags the process-specific case so the streamer can PREFACE it with a
# brief honest caveat instead. Simple + documented on purpose: markers are
# org-possessives ("our/my", "the company/team/…") and process/policy nouns
# ("policy/process/procedure/SOP/onboarding/refund/approval/…"), EN + IT. A false
# positive only adds a caveat; a false negative only omits it — both safe.
_PROCESS_SPECIFIC = re.compile(
    r"\b("
    # org-possessives — this company's OWN thing
    r"our|ours|my|company'?s|team'?s|"
    r"the\s+(?:company|team|org|organi[sz]ation|firm|fund|business|office)|"
    # process / policy nouns
    r"policy|policies|process(?:es)?|procedures?|sop|sops|workflow|guidelines?|"
    r"onboarding|offboarding|approvals?|refunds?|reimburse\w*|escalation|"
    r"runbook|playbook|checklist|protocol|"
    # Italian
    r"nostr[oaie]|mia|mio|miei|mie|"
    r"la\s+(?:nostra\s+)?(?:azienda|societ[àa]|ditta)|"
    r"politich?e?|policy|procedur\w*|processo|processi|flusso|"
    r"approvazione|rimbors\w*|linee\s+guida|prassi|protocollo"
    r")\b",
    re.IGNORECASE,
)

# The caveat prefix itself, spoken as a lead-in before the world-knowledge
# answer. Ends with an em-dash so it flows straight into the answer.
_CAVEAT_UNGROUNDED_EN = "I don't see this in your process docs, so answering generally —"
_CAVEAT_UNGROUNDED_IT = (
    "Non lo trovo nei vostri documenti di processo, quindi rispondo in generale —"
)


def _looks_process_specific(question: str) -> bool:
    """True when a question reads as being about THIS company's own process/
    policy (something that SHOULD come from the docs). See _PROCESS_SPECIFIC.
    Cheap + only consulted when retrieval already fell below the floor, so it
    never touches the grounded happy path."""
    return bool(_PROCESS_SPECIFIC.search(question or ""))


def _ungrounded_process_caveat(question: str, below_floor: bool) -> str:
    """The caveat lead-in for a below-floor PROCESS-specific answer, or "" when
    it does not apply (feature off, chunks were grounded, or a general/world
    question). Language follows the asker."""
    if not (settings.caveat_ungrounded_process_answers and below_floor):
        return ""
    if not _looks_process_specific(question):
        return ""
    return _CAVEAT_UNGROUNDED_IT if sounds_italian(question) else _CAVEAT_UNGROUNDED_EN


def answer_question_stream(
    avatar: Avatar,
    question: str,
    *,
    history: str = "",
    memory: str = "",
    state: "meeting_state.MeetingState | None" = None,
    summary: str = "",
    speaker: str = "",
    roster: "list[str] | None" = None,
    k: int = 6,
    min_chars: int = 0,
    meta: "dict | None" = None,
    mission: str = "",
    org_id: str = "",
):
    """Yield spoken sentences as they are generated. Yields nothing (stays silent)
    only when the model judges the speech was not addressed to Laura (SKIP).

    `mission` is the optional per-meeting objective (admin-set, or the avatar's
    default) she keeps in mind and raises if left unmet — folded into the system
    prompt as an instruction only, so the caller's turn-taking / hand-raise gate
    still owns WHEN she speaks. "" = no mission = today's prompt exactly.

    ``meta`` (optional out-param) is filled — before the first sentence — with
    ``{"top_score": <grounding confidence>}``: the top retrieved-chunk score that
    survived the ``rag_min_context_score`` gate (0.0 when nothing grounded). It
    exposes the confidence the retrieval step ALREADY computed so a caller (the
    hand-raise interjection escape) can gate on it without a second model call.

    `memory` is the cross-meeting carryover brief (ledger.carryover_brief):
    what previous sessions of this same meeting left open or decided. Empty
    for first-time meetings — the prompt then carries no memory block at all.

    `state` is the live MeetingState tracker (regex-built, already in memory —
    zero latency cost). It holds exactly what "what did we decide / who owns X /
    what's missing?" questions need, which the recent-history window alone can't
    answer. Injected only when it has signal, so quiet meetings add no noise.

    `summary` is the rolling notes of the meeting OLDER than the recent-history
    window (kept fresh in the background) — the whole meeting's arc without
    widening the hot-path prompt.

    `speaker` is who said this line and `roster` who is in the room right now
    (from Recall participant events — includes people who never spoke). They
    make her multi-party aware: address the asker by name, answer "how many
    are we?", and SKIP lines aimed at another named participant.

    `min_chars>0` coalesces tiny sentences ("Yes." "Sure.") into a chunk of at
    least that many characters before yielding, so the TTS voice flows instead of
    stuttering one fragment at a time (a touch more first-audio latency for
    smoother prosody). The FIRST chunk uses a lower threshold — the opening words
    are what the room is waiting on — and later chunks keep the full min_chars
    for smooth prosody.
    """
    _t0 = time.perf_counter()
    chunks = _retrieve_for(avatar, question, history, k, org_id=org_id)
    _retrieve_ms = (time.perf_counter() - _t0) * 1000
    # Only ground in the docs when they actually match the question —
    # irrelevant chunks bias the model into doc-quoting general answers.
    # Remember WHY they were dropped: chunks existed but scored below the floor
    # (thin retrieval) is exactly the case where a company/process-specific
    # answer would otherwise read as doc-grounded — it earns an honest caveat.
    _below_floor = bool(chunks) and chunks[0].score < settings.rag_min_context_score
    if _below_floor:
        chunks = []
    citation = chunks[0].source if chunks else ""
    # Expose the grounding confidence the retrieval already computed (top
    # surviving chunk score, 0.0 when nothing grounded) for a caller that gates
    # on it — set BEFORE the first yield so it is populated once iteration ends.
    if meta is not None:
        meta["top_score"] = float(chunks[0].score) if chunks else 0.0

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
    # The silent tracker: decisions, owners, deadlines, covered/missing process
    # steps. Only injected when it actually tracked something — an empty scaffold
    # ("type: unknown") would just bias her toward process-speak on small talk.
    state_block = (
        f"Laura's own silent meeting notes (tracked live — trust these):\n"
        f"{meeting_state.state_summary(state)}\n\n"
        if state is not None and _state_has_signal(state)
        else ""
    )
    summary_block = (
        f"Running summary of this meeting so far (before the recent lines below):\n"
        f"{summary}\n\n"
        if summary.strip()
        else ""
    )
    context_block = (
        f"Company/fund document context (relevant to this question):\n\n{_format_context(chunks)}\n\n"
        if chunks
        else ""
    )
    # Live roster (Recall participant events): includes people who never spoke,
    # which the transcript alone can't see. One short line — latency-neutral.
    roster_block = _roster_block(avatar, roster, state)
    asker = (speaker or "").strip() or "Someone"
    # {name} parameterizes the previously hardcoded "You are Laura" — for the
    # avatar actually speaking (byte-identical when that avatar IS Laura), and
    # for org display-name overlays (M2) which land here via avatar.name.
    system = ANSWER_STREAM_SYSTEM.format(
        persona=avatar.persona_prompt, name=avatar.name
    ) + _mission_directive(mission)
    user = (
        f"{context_block}"
        f"{state_block}"
        f"{summary_block}"
        f"{remembered}"
        f"{roster_block}"
        f"{convo}"
        f"{asker} in the meeting just said:\n{question}\n\n"
        f"Answer in spoken style. Reply SKIP only if this was clearly not directed at {avatar.name}."
    )

    pending = ""      # confirmed answer text not yet flushed as a whole sentence
    outbuf = ""       # whole sentences merged toward a min_chars chunk (cadence)
    decided = False   # whether we've ruled out the SKIP sentinel
    spoke_any = False
    _first_token_ms = None
    # Honest caveat for a below-floor PROCESS-specific answer, spoken as a lead-in
    # the moment SKIP is ruled out (so a SKIP still stays fully silent, and the
    # grounded/above-floor path yields ""  → zero cost). Emitted at most once.
    _caveat_line = _ungrounded_process_caveat(question, _below_floor)
    _caveat_emitted = False

    def _log_first() -> None:
        if not spoke_any:
            print(
                f"[latency] answer_stream retrieve={_retrieve_ms:.0f}ms "
                f"first_token={_first_token_ms:.0f}ms "
                f"first_sentence={(time.perf_counter() - _t0) * 1000:.0f}ms",
                flush=True,
            )

    _provider, _model = _live_route(question)
    if _provider == "search":
        # No ungrounded-process caveat here: this branch answers from a live web
        # search, and the announce line ("let me look that up") already discloses
        # the answer isn't from the docs — a second caveat would be redundant.
        # Announce the lookup BEFORE the slow web call — it buys the search its
        # seconds honestly instead of leaving dead air. In the asker's language.
        yield random.choice(
            _SEARCH_ANNOUNCE_IT if sounds_italian(question) else _SEARCH_ANNOUNCE_EN
        )
        answer = _web_search_answer(question, convo)
        if answer:
            buf, sentences = _split_sentences(answer + " ")
            for sent in sentences:
                yield sent
            if buf.strip():
                yield buf.strip()
            return
        # Search flaked (returned nothing / refused) — fall THROUGH to the fast
        # model so she still answers from her own knowledge instead of going
        # silent.
        question = f"{question} (You could not search the web just now — answer from your knowledge and say it may not be current.)"
        _provider, _model = settings.brain_provider, settings.brain_model_fast
    _max_tokens = 400
    for delta in llm.stream_complete(
        system, user, max_tokens=_max_tokens, model=_model, provider=_provider
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
            # SKIP is ruled out → she IS answering. If this is a below-floor
            # process-specific answer, lead with the honest caveat before any
            # world-knowledge content, so it never reads as doc-grounded.
            if _caveat_line and not _caveat_emitted:
                _caveat_emitted = True
                _log_first()
                yield _caveat_line
                spoke_any = True

        pending, sentences = _split_sentences(pending)
        for s in sentences:
            if min_chars > 0:
                outbuf = f"{outbuf} {s}".strip()
                # First chunk: lower bar — those opening words are the perceived
                # latency. Later chunks keep min_chars for smooth prosody.
                need = min_chars if spoke_any else min(min_chars, _FIRST_CHUNK_MIN_CHARS)
                if len(outbuf) >= need:
                    _log_first()
                    yield outbuf
                    spoke_any = True
                    outbuf = ""
            else:
                _log_first()
                yield s
                spoke_any = True

    # Flush whatever is left: buffered whole sentences plus any partial tail.
    tail = pending.strip()
    remainder = f"{outbuf} {tail}".strip() if min_chars > 0 else tail
    if remainder and (decided or not _is_skip(remainder)):
        # A very short answer can flush only here (never set `decided` in the
        # loop) — still lead with the caveat if it applies and wasn't emitted.
        if _caveat_line and not _caveat_emitted:
            _caveat_emitted = True
            yield _caveat_line
        _log_first()
        yield remainder
        spoke_any = True

    # Citation is not auto-appended: it made small talk read absurdly ("nice joke
    # — per onboarding_sop.md"). The model is instructed to name the source doc
    # itself when (and only when) it actually answers from a process document.


# ─────────────────── rolling meeting notes (background) ───────────────────
# Keeps the live brain aware of the WHOLE meeting: the hot-path prompt carries
# only the last few lines, so everything older is folded into short running
# notes off the hot path (fast model, called from a background task in main).
ROLLING_SUMMARY_SYSTEM = """You maintain running notes of a live work meeting \
for an assistant who is in the room. Merge the existing notes with the new \
transcript lines into ONE updated set of notes, at most 120 words. Keep only \
what stays useful later: topics discussed, decisions, owners, deadlines, \
numbers, blockers, and open questions. Drop small talk and filler. Plain \
text, no markdown, no preamble — return the updated notes only."""


def rolling_summary(avatar: Avatar, prior: str, new_lines: str) -> str:
    """Fold new transcript lines into the running notes. Returns the updated
    notes, or "" on stub/error (the caller then keeps the old notes)."""
    if _is_stub():
        return ""  # keyless demo: no model — the recent-history window suffices
    try:
        raw = llm.complete(
            ROLLING_SUMMARY_SYSTEM,
            (
                f"Existing notes:\n{prior.strip() or '(none yet)'}\n\n"
                f"New transcript lines:\n{new_lines}\n\n"
                "Updated notes:"
            ),
            max_tokens=260,
            model=settings.brain_model_fast,
        )
    except Exception as e:  # noqa: BLE001 — notes are a bonus, never a failure
        print(f"[notes] rolling summary failed: {e}", flush=True)
        return ""
    return (raw or "").strip()[:1600]


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

You are in a live spoken conversation — a capable general assistant FIRST \
(think ChatGPT or Claude), and a company/process expert only when the question \
actually touches that. Default to 1-2 short spoken sentences (3 max); sound like \
a real person, never restate the question, and never open with filler like \
"great question". Plain text only — no markdown, bullets, headings, or preamble. \
NEVER mention documents, context, a knowledge base, or your "system architecture" \
unless the person specifically asks how you work.

How to respond:
- General questions, opinions, advice, small talk, jokes: answer directly and \
naturally from your own knowledge. If someone asks what you think, give a real \
take. Don't steer the conversation toward work topics nobody asked about.
- Company / process / portfolio questions: use what you know, stay concrete, and \
don't invent specific numbers, companies, or facts that aren't there.

You can also USE TOOLS when they make an answer more concrete:
- calculator — for any arithmetic (percentages, totals, per-seat cost, annualizing).
- date_math — today's date, or days until a deadline/renewal.
- lookup_record — check a customer account (plan, seats, MRR, renewal, owner).
- queue_action — when someone asks YOU to do something (send, schedule, book, \
create, check, remind): queue it. Actions run AFTER the call behind an approval \
— confirm it's queued, and NEVER claim it was already done.
Call a tool whenever it helps — you may chain them — then state the concrete \
result plainly in a sentence or two."""


def answer_with_tools(
    avatar: Avatar, question: str, *, history: str = "", k: int = 6, session=None
) -> dict:
    """Grounded answer that may CALL tools to act. Returns answer + tools_used.

    `session` (optional) is the live store.Session: it is threaded into the
    tool dispatch so session-aware tools (queue_action) can capture onto it —
    session=None keeps the exact pre-existing behavior."""
    convo = f"Recent conversation:\n{history}\n\n" if history.strip() else ""

    # Web search for questions that want fresh/current info — Claude's native
    # web_search tool (same provider-neutral path the meeting path uses). Falls
    # through to normal tool-using reasoning if search errors or returns nothing.
    if not _is_stub() and wants_web_search(question):
        answer = _web_search_answer(question, convo)
        if answer:
            # Route name only — the question is live-meeting content (PII).
            print("[search] tool-path question answered from web", flush=True)
            return {
                "answer": answer,
                "tools_used": [{"tool": "web_search", "args": {}, "result": ""}],
                "citations": [],
            }

    chunks = _retrieve_for(avatar, question, history, k)
    # Only inject docs when they actually match the question — otherwise irrelevant
    # chunks framed as "context" bias her into doc-quoting a general/opinion ask.
    if chunks and chunks[0].score < settings.rag_min_context_score:
        chunks = []

    if _is_stub():
        # No tool use offline — fall back to the deterministic grounded answer.
        r = _stub_answer(chunks)
        return {"answer": r["answer"], "tools_used": [], "citations": r.get("citations", [])}
    context_block = (
        f"Context you may draw on if it fits the question:\n\n{_format_context(chunks)}\n\n"
        if chunks
        else ""
    )
    system = ANSWER_TOOLS_SYSTEM.format(persona=avatar.persona_prompt)
    user = (
        f"{context_block}"
        f"{convo}"
        f"Someone asked:\n{question}\n\n"
        "Use tools if they'd help, then answer in spoken style."
    )
    used: list = []
    try:
        text, used = llm.complete_with_tools(
            system,
            user,
            # LIVE meeting path: native tools + Cedric read+fast tools only
            # (the latency contract); Cedric calls run on the ~2s budget.
            tools.specs_for(session, live=True),
            tools.dispatch_for(session, live=True),
            model=settings.brain_model_fast,
        )
    except Exception as e:  # noqa: BLE001 — Groq tool endpoint 429s/errors have no fallback
        print(f"[tools] complete_with_tools failed ({e}); plain answer", flush=True)
        text = ""
    if used:
        # Tool names only — the question is live-meeting content (PII).
        print("[tools] used: " + ", ".join(u["tool"] for u in used), flush=True)
    answer = (text or "").strip()
    if not answer:
        # The tool path can raise (Groq's tool endpoint rate-limits with no
        # fallback) or return empty (Groq llama does this intermittently). Never go
        # silent on the interactive avatar: retry as a PLAIN answer — llm.complete
        # falls back to Claude Haiku when Groq fails — and if that's still empty,
        # say something rather than leaving dead air.
        try:
            answer = (llm.complete(system, user, model=settings.brain_model_fast) or "").strip()
        except Exception:
            answer = ""
        if not answer:
            answer = "Sorry, I didn't catch that — could you say it again?"
    return {
        "answer": answer,
        "tools_used": used,
        "citations": [chunks[0].source] if chunks else [],
    }


# ───────────────────────── post-meeting ─────────────────────────────
POSTMEETING_SYSTEM = """You analyze a meeting transcript against company \
process knowledge. Produce a crisp post-meeting artifact a team can act on.

SOURCE BOUNDARY: the MEETING TRANSCRIPT is the only source for decisions, \
actions, and risks that were actually raised or agreed in this meeting. A \
pre-meeting brief and company process context are background only: use them to \
understand the meeting and describe process gaps, but NEVER turn their agenda, \
open items, deadlines, or suggested next steps into decisions, actions, or \
risks unless the transcript itself discusses them.

Detect PROCESS GAPS, specifically any of: missing owner, missing deadline, \
missing approval, missing required document, unresolved blocker. Only flag a \
gap if it is genuinely implied by the discussion; do not pad the list.

Capture EVERY action anyone asked for or committed to as an actions[] entry — \
including short, in-passing requests ("send the recap", "schedule a follow-up \
with Marco", "post it to Slack", "email Priya") — even when no owner or deadline \
was stated (use "UNASSIGNED"/"" and gap_type accordingly). Do not drop an action \
just because it was said casually. For each extracted action, include a short, \
verbatim evidence excerpt copied from the MEETING TRANSCRIPT. If there is no \
supporting transcript excerpt, do not emit the action.

If the prompt lists actions ALREADY CAPTURED LIVE during the meeting, those are \
already queued for execution: do NOT put them (or any semantically equivalent \
restatement) into actions[]. Equivalence is by MEANING, not wording — a \
translation counts (an Italian live capture and its English restatement are the \
SAME action; re-listing it would execute it twice). Only add actions that are \
genuinely new relative to that list.

Return ONLY a JSON object:
{
  "summary": "<3-5 sentence plain summary of what was discussed and decided>",
  "decisions": ["<each decision the group actually reached, one short line>"],
  "actions": [
    {"item": "<action>", "owner": "<name or 'UNASSIGNED'>", "deadline": "<stated deadline or ''>", "gap_type": "<owner|deadline|approval|document|blocker|none>", "evidence": "<exact supporting excerpt from the meeting transcript>"}
  ],
  "risks": ["<each risk or unresolved blocker raised, one short line>"],
  "follow_up_email": {
    "subject": "<subject line>",
    "body": "<short professional email body summarizing decisions and next steps>"
  }
}"""


def _scope_actions_to_transcript(artifact: dict, transcript_text: str) -> None:
    """Keep only model-extracted actions grounded in the meeting transcript.

    The post model also sees a pre-meeting brief and process context so it can
    produce a useful summary and gap analysis. Those background sections are
    deliberately not action sources. Requiring a verbatim transcript excerpt
    gives us a deterministic boundary after the model call: a brief-only item
    cannot enter the artifact merely because the model ignored the prompt.

    Live ``queue_action`` captures do not pass through this filter. They are
    merged later by ``main._merge_action_items`` and remain authoritative.
    """
    transcript = " ".join((transcript_text or "").split()).casefold()
    actions = artifact.get("actions") or artifact.get("checklist") or []
    scoped: list[dict] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        evidence = " ".join(str(action.get("evidence") or "").split()).casefold()
        if not evidence or evidence not in transcript:
            continue
        clean = dict(action)
        clean.pop("evidence", None)
        scoped.append(clean)
    artifact["actions"] = scoped
    artifact["checklist"] = scoped


def _live_actions_block(live_actions: list[dict] | None) -> str:
    """The 'already captured live' section of the post-meeting prompt.

    One line per queue_action capture (action + any owner/due), under a header
    that repeats the do-not-re-extract rule next to the data it applies to.
    "" when there were no live captures — the prompt is unchanged for the
    common no-capture meeting.
    """
    lines = []
    for a in live_actions or []:
        text = (a.get("action") or "").strip()
        if not text:
            continue
        owner = (a.get("owner") or "").strip()
        due = (a.get("due") or "").strip()
        suffix = (f" (owner: {owner})" if owner else "") + (
            f" (due: {due})" if due else ""
        )
        lines.append(f"- {text}{suffix}")
    if not lines:
        return ""
    return (
        "Actions ALREADY CAPTURED LIVE during the meeting (each already has an "
        "approval card — do NOT re-extract these, nor any rephrasing or "
        "translation of them; only genuinely new actions belong in actions[]):\n"
        + "\n".join(lines)
        + "\n\n"
    )


def _task_hints_block(tasks: list[dict] | None) -> str:
    """The 'known task types' section of the post-meeting prompt: deterministic
    task hints (avatar.yaml ``tasks``) that BIAS action capture toward this
    avatar's recognized asks. Each hint pairs trigger phrases with the action
    intent to record.

    This never fabricates: the SOURCE BOUNDARY and the verbatim-evidence rule
    still apply, so a hint only lands as an action when the TRANSCRIPT actually
    contains that ask (``_scope_actions_to_transcript`` drops the rest). "" when
    the avatar defines no tasks — the prompt is unchanged for every avatar
    without hints.
    """
    lines = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        triggers = [str(x).strip() for x in (t.get("triggers") or []) if str(x).strip()]
        action = (t.get("action") or "").strip()
        label = action or (t.get("name") or "").strip()
        if not label or not triggers:
            continue
        quoted = "; ".join(f'"{p}"' for p in triggers)
        lines.append(f"- {label} — recognized when someone asks for something like {quoted}.")
    if not lines:
        return ""
    return (
        "KNOWN TASK TYPES this avatar can act on. If the MEETING TRANSCRIPT shows "
        "the group asked for or agreed to any of these (even briefly or in "
        "passing), make sure it appears in actions[], worded to match the task's "
        "action, with the verbatim transcript excerpt as evidence. Do NOT invent "
        "one the transcript does not support:\n"
        + "\n".join(lines)
        + "\n\n"
    )


# Safety net behind the prompt-level prevention above: the summarizer is a
# model, so "do not re-extract" is obeyed almost always, not always. The merge
# in main._merge_action_items catches same-language rephrases by content-word
# overlap, but an Italian live capture and its English re-extraction share no
# words — that gap produced a real double execution (two approval cards → two
# calendar events for one spoken request). This ONE cheap completion closes it.
ACTION_DEDUP_SYSTEM = """You compare two lists of action items from the same \
meeting: LIVE actions (captured in the room, already queued for execution) and \
EXTRACTED actions (from a post-meeting summary). Identify every EXTRACTED \
action that is the SAME real-world request as one of the LIVE actions — same \
task and same target — even when it is worded differently or written in a \
DIFFERENT LANGUAGE (e.g. an Italian live capture restated in English). \
Executing both entries of a matched pair would do the task twice, so match on \
meaning, not wording. Be conservative: if two items could plausibly be two \
distinct tasks, do not match them.

Return ONLY a JSON object:
{"duplicates": [{"extracted": <index into EXTRACTED>, "live": <index into LIVE>}]}
Return {"duplicates": []} when nothing matches."""


def semantic_action_duplicates(
    live: list[str], extracted: list[str]
) -> list[tuple[int, int]]:
    """(extracted_idx, live_idx) pairs where a summarizer-extracted action is
    semantically the same request as a live capture — cross-language included.

    Called by the finalize merge only for the extracted actions that survived
    the word-overlap dedup, and only when live captures exist — so in the
    common case (no live captures, or the summarizer obeyed the prompt-level
    prevention) it costs nothing or one small completion, always OFF the live
    path. Fails OPEN: stub mode, a model error, or junk output returns [] —
    a missed dedup is a reviewable duplicate card, a false merge would silently
    drop a real action.
    """
    if not live or not extracted or post_provider() == "stub":
        return []
    live_lines = "\n".join(f"{i}. {t}" for i, t in enumerate(live))
    extracted_lines = "\n".join(f"{i}. {t}" for i, t in enumerate(extracted))
    try:
        raw = llm.complete(
            ACTION_DEDUP_SYSTEM,
            (
                f"LIVE actions:\n{live_lines}\n\n"
                f"EXTRACTED actions:\n{extracted_lines}\n\n"
                "Respond with the JSON object only."
            ),
            max_tokens=400,
            provider=post_provider(),
        )
        duplicates = _parse_json(raw).get("duplicates") or []
    except Exception:  # noqa: BLE001 — fail open, never break finalize
        return []
    pairs: list[tuple[int, int]] = []
    for d in duplicates:
        try:
            xi, li = int(d["extracted"]), int(d["live"])
        except (TypeError, KeyError, ValueError):
            continue  # junk entry from the model: skip it, keep the rest
        if 0 <= xi < len(extracted) and 0 <= li < len(live):
            pairs.append((xi, li))
    return pairs


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
    mission: str = "",
) -> dict:
    """Decide if the avatar should proactively flag ONE missing step. Default: no.

    When the tracked MeetingState says a critical required process step never
    happened, this is deterministic — the templated intervention line goes out
    with no model call (reliable in stub AND Claude mode, zero extra latency).
    Otherwise the model judges from the transcript, with the structured state
    as extra grounding.

    `mission` is the optional per-meeting objective: on the model path it is
    folded into the system prompt so an unmet mission is a valid thing to raise
    as the call wraps up, alongside a missing process step. "" leaves the prompt
    unchanged (a missing critical step still short-circuits deterministically).
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
        PROACTIVE_SYSTEM.format(persona=avatar.persona_prompt) + _mission_directive(mission),
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


def post_meeting(
    avatar: Avatar,
    transcript_text: str,
    *,
    k: int = 6,
    context: str = "",
    live_actions: list[dict] | None = None,
    state: "meeting_state.MeetingState | None" = None,
) -> dict:
    """Full post-meeting artifact: summary, decisions, actions, missing process
    steps, readiness score, risks, and a draft follow-up email.

    The tracked MeetingState (rebuilt from the transcript) supplies the
    deterministic parts — missing_steps and readiness_score come from the
    process template, not model judgement — and backfills decisions/risks when
    the model returns none. `context` is an optional pre-meeting brief (the
    orchestrator's agenda/participants/open items) so the summary understands
    what the meeting was FOR. It is never an action source: model actions must
    carry verbatim transcript evidence and are filtered against the transcript.

    `live_actions` are the session's queue_action captures (action/owner/due
    dicts). They are shown to the model with an explicit do-not-re-extract
    instruction — dedup PREVENTION at the source. Without it the summarizer
    re-extracts a live capture in ITS OWN words (often translating an Italian
    ask into English), the downstream word-overlap dedup can't bridge the
    language gap, and one spoken request becomes two approval cards → double
    execution. Finalize-only: this never touches the live path.
    """
    if state is None:
        state = meeting_state.build_from_text(avatar, transcript_text)

    if post_provider() == "stub":
        artifact = _stub_post_meeting(avatar, transcript_text, state)
    else:
        # Ground gap-detection in the actual process docs.
        chunks = retrieve(
            avatar, transcript_text[-3000:] or "process steps owners approvals", k=k
        )
        brief_block = (
            "BACKGROUND ONLY — PRE-MEETING BRIEF (not evidence of what was "
            f"discussed or agreed):\n\n{context}\n\n"
            if context.strip()
            else ""
        )
        live_block = _live_actions_block(live_actions)
        # Deterministic task hints (avatar.yaml `tasks`): bias action capture
        # toward this avatar's recognized asks. "" when the avatar has no tasks.
        task_block = _task_hints_block(getattr(avatar, "tasks", None))
        raw = llm.complete(
            POSTMEETING_SYSTEM,
            (
                f"{brief_block}"
                f"{live_block}"
                f"{task_block}"
                f"Relevant company process context:\n\n{_format_context(chunks)}\n\n"
                f"Structured meeting state (tracked during the meeting):\n"
                f"{meeting_state.state_summary(state)}\n\n"
                f"Meeting transcript:\n\n{transcript_text}\n\n"
                "Respond with the JSON object only."
            ),
            max_tokens=4000,
            provider=post_provider(),
        )
        artifact = _parse_json(raw)
        if _looks_degraded(artifact):
            # The post model returned malformed/empty JSON (e.g. it truncated, or
            # thinking ate the budget). NEVER ship a blank recap: rebuild
            # deterministically from the silent tracker so summary + email +
            # actions are always populated. A model hiccup degrades to a real
            # (if plainer) artifact, not "".
            print(
                "[post_meeting] degraded model output; "
                "rebuilding recap from the deterministic tracker",
                flush=True,
            )
            artifact = _stub_post_meeting(
                avatar, transcript_text, state, degraded=True
            )
        else:
            _scope_actions_to_transcript(artifact, transcript_text)

    return _finish_artifact(artifact, state)


def degraded_post_meeting(
    avatar: Avatar,
    transcript_text: str,
    *,
    state: "meeting_state.MeetingState | None" = None,
) -> dict:
    """A complete deterministic recap for when the post-meeting model call FAILS
    outright (a transient 429/529/timeout that re-raises) rather than returning
    malformed JSON.

    post_meeting() already rebuilds a real recap from the silent tracker when the
    model returns unusable JSON; this reuses that exact path for the harder case
    where the model call itself raised — so a finalize-time model hiccup DEGRADES
    to a plain (but full) artifact — summary + follow-up email + actions — instead
    of losing the whole deliverable. Off the live path (finalize only)."""
    if state is None:
        state = meeting_state.build_from_text(avatar, transcript_text)
    artifact = _stub_post_meeting(avatar, transcript_text, state, degraded=True)
    return _finish_artifact(artifact, state)


# A decision/risk is one short line. The model occasionally (a) emits malformed
# JSON — _parse_json then returns an {"answer": <raw>} shape with no summary —
# or (b) echoes whole transcript chunks into a list field. Either way garbled
# text must never reach the recap email / notes doc, so list fields are cleaned
# and a degraded artifact is rebuilt from the deterministic silent tracker.
_MAX_LINE_CHARS = 240
_SPEAKER_RE = re.compile(r"[A-Z][a-zA-Z]+:\s")


def _clean_lines(items: object) -> list[str]:
    """Keep only genuine one-liners: strings, non-empty, not transcript-shaped
    (too long, or carrying multiple 'Speaker:' labels)."""
    if not isinstance(items, (list, tuple)):
        return []
    out: list[str] = []
    for it in items:
        s = (it if isinstance(it, str) else str(it)).strip()
        if not s or len(s) > _MAX_LINE_CHARS or len(_SPEAKER_RE.findall(s)) >= 2:
            continue
        out.append(s)
    return out


def _looks_degraded(artifact: dict) -> bool:
    """A parse-failure fallback ({"answer": ...} with no real summary/actions)."""
    return (
        "answer" in artifact
        and not (artifact.get("summary") or "").strip()
        and not artifact.get("actions")
        and not artifact.get("checklist")
    )


# When a process template matched the meeting, readiness = the share of required
# process steps it covered (state.readiness_score()). When NO template matched
# (e.g. a generic non-onboarding transcript) that score is undefined and comes
# back 0 — leaving the demo readiness tile a dead "—". Derive instead a
# defensible OUTCOME-readiness from the distilled artifact alone: did the meeting
# produce a recap, action items, owners, and a drafted follow-up? Deterministic
# (same artifact → same score), no model call, no new artifact keys. Weights sum
# to 100.
_READY_SUMMARY_W = 25   # a real recap was distilled
_READY_ACTIONS_W = 25   # at least one action item was captured
_READY_OWNED_W = 30     # captured actions carry named owners (scaled by ratio)
_READY_FOLLOWUP_W = 20  # a follow-up email was drafted


def _action_has_owner(action: object) -> bool:
    """True when an action item names a real owner (not blank / UNASSIGNED)."""
    if isinstance(action, dict):
        owner = str(action.get("owner") or "").strip()
        return bool(owner) and owner.upper() != "UNASSIGNED"
    return False


def _derived_readiness(artifact: dict) -> int:
    """Defensible 0-100 outcome-readiness for a meeting with no process template
    (see the weights above). Reads only distilled artifact fields, so it never
    touches the transcript and is safe on the demo path."""
    score = 0
    if (artifact.get("summary") or "").strip():
        score += _READY_SUMMARY_W
    actions = artifact.get("actions") or []
    if actions:
        score += _READY_ACTIONS_W
        owned = sum(1 for a in actions if _action_has_owner(a))
        score += round(_READY_OWNED_W * owned / len(actions))
    email = artifact.get("follow_up_email") or {}
    if (str(email.get("subject") or "") + str(email.get("body") or "")).strip():
        score += _READY_FOLLOWUP_W
    return min(100, score)


def _finish_artifact(artifact: dict, state: "meeting_state.MeetingState") -> dict:
    """Normalize to the full artifact schema; state fills the deterministic
    fields and backfills anything the model left out.

    NOTE: post_meeting() already recovers a degraded model result into a real
    deterministic recap (summary + email + actions) before calling this, so the
    guard below is now a defensive backstop only. It must not be the primary
    degrade path — dropping to {} here leaves summary "" (the empty-summary bug);
    a non-empty recap has to come from post_meeting."""
    if _looks_degraded(artifact):
        artifact = {}  # backstop: post_meeting normally intercepts this first
    artifact.setdefault("summary", "")
    artifact.setdefault("follow_up_email", {})
    # Clean any model-supplied list fields, then backfill from state when empty.
    artifact["decisions"] = _clean_lines(artifact.get("decisions")) or [
        d["decision"] for d in state.decisions
    ]
    artifact["risks"] = _clean_lines(artifact.get("risks")) or [
        r["risk"] for r in state.risks
    ]
    # Old consumers (demo page, Slack formatter) read "checklist"; new schema
    # calls it "actions". Keep both pointing at the same list.
    actions = artifact.get("actions") or artifact.get("checklist") or []
    artifact["actions"] = actions
    artifact["checklist"] = actions
    artifact["missing_steps"] = list(state.missing_steps)
    # Readiness: the rigorous template-coverage score when a process template
    # matched; otherwise a defensible outcome-readiness derived from the artifact
    # so the tile is never a dead "—" for a non-onboarding meeting.
    artifact["readiness_score"] = (
        state.readiness_score()
        if state.required_steps
        else _derived_readiness(artifact)
    )
    artifact["meeting_type"] = state.meeting_type
    # Participation view (Read.ai-style, but in the same product as the voice):
    # per-person talk share + what each person committed to. Straight from the
    # silent tracker — no extra model call.
    total_lines = sum(p["lines"] for p in state.per_person.values()) or 1
    artifact["participation"] = [
        {
            "name": str(p.get("name") or participant_key),
            "lines": p["lines"],
            "talk_share": round(100 * p["lines"] / total_lines),
            "commitments": list(p["commitments"]),
        }
        for participant_key, p in sorted(
            state.per_person.items(), key=lambda kv: -kv[1]["lines"]
        )
    ]
    return artifact


# ─────────────── typed-action producer (native executor) ────────────
# The native executor (backend/app/executor.py) will run an APPROVED ledger
# action only when it carries a TYPED spec — one of:
#   {"type": "calendar.create_event", "args": {title, start, end, attendees?}}
#   {"type": "email.send",            "args": {to, subject, body}}
# type_actions() annotates the finalized artifact actions[] with such a spec
# where an item CLEARLY maps, at finalize (off the live path). It mirrors the
# summarizer's no-invented-recipients boundary: a recipient email is emitted
# ONLY when it literally appears in that action's distilled text (item / owner /
# deadline / summary brief), never guessed; a start/end must be a real ISO-8601
# datetime. An item that does not clearly map stays generic (no `typed`), so the
# dashboard still shows it and the Cedric path is unaffected. The raw transcript
# is NOT passed here — only already-distilled fields flow — so this adds no PII
# surface beyond what post_meeting already sent to the post model.
TYPED_ACTION_SYSTEM = """You convert a meeting's action items into typed, \
executable specs, but ONLY when an item unambiguously maps to one of the two \
supported actions AND every required field is present in the SOURCE TEXT given \
for that item. The two types:

- "calendar.create_event": schedule a meeting/call. Required args: title, \
start, end (both full ISO-8601 date-times, e.g. 2026-08-01T15:00:00). \
attendees is optional (email addresses only). Use TODAY (given below) only to \
resolve a date/time the item itself states ("Friday 3pm"); if the item states \
no concrete time, DO NOT emit this type.
- "email.send": send an email. Required args: to (one or more email addresses \
that LITERALLY appear in the item's source text), subject. body is optional.

HARD RULES (precision over recall):
- NEVER invent a recipient, an email address, a date, or a time. Use ONLY \
values that literally appear in the item's source text (you may normalise a \
stated date/time to ISO using TODAY). If a required field is not present, DO \
NOT emit a type for that item — leave it untyped.
- If you are not sure, leave it untyped.

Return ONLY a JSON object mapping the 0-based item index (as a string) to its \
typed spec, omitting every item that does not map:
{"0": {"type": "email.send", "args": {"to": ["a@b.com"], "subject": "...", "body": "..."}}}"""

# Appended to TYPED_ACTION_SYSTEM only when the org's Asana is connected and
# the acting avatar may use it (type_actions allow_asana): action items become
# proposed Asana tasks. Deliberately the LAST resort type — an item that maps
# to calendar/email keeps that mapping.
TYPED_ACTION_ASANA = """

A third type is also supported for this meeting:
- "asana.create_task": record an action item as a task in the team's Asana. \
Required args: name (a short imperative task title drawn from the item). \
Optional: notes (one sentence of context from the item), assignee (an email \
address that LITERALLY appears in the item's source text), due_on \
(YYYY-MM-DD — only when the item states a concrete date), project (a project \
name that LITERALLY appears in the item's source text or the meeting summary).
- Prefer calendar.create_event / email.send when an item maps to those; use \
asana.create_task for every OTHER item that is a discrete piece of work \
someone agreed to do. Do not create tasks for vague remarks, questions, or \
things already done."""

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ISO_DT_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?")
_EMAIL_INTENT_RE = re.compile(
    r"\b(e-?mail|send (?:an? )?(?:email|note|recap|the recap)|reply to|write to)\b",
    re.IGNORECASE,
)
_CAL_INTENT_RE = re.compile(
    r"\b(schedule|calendar|set up (?:a )?(?:meeting|call)|book (?:a )?(?:meeting|call)|"
    r"invite .* to|follow-?up (?:meeting|call))\b",
    re.IGNORECASE,
)

CALENDAR_CREATE = "calendar.create_event"
EMAIL_SEND = "email.send"
ASANA_CREATE = "asana.create_task"
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _grounded_emails(value: object, source: str) -> list[str]:
    """Emails from ``value`` (str or list) that LITERALLY appear in ``source`` —
    the mechanical no-invented-recipients guard. Case-insensitive, de-duped,
    order-preserving."""
    src = (source or "").lower()
    raw: list[str] = []
    if isinstance(value, str):
        raw = _EMAIL_RE.findall(value)
    elif isinstance(value, (list, tuple)):
        for v in value:
            raw.extend(_EMAIL_RE.findall(str(v)))
    out: list[str] = []
    for e in raw:
        e = e.strip()
        if e and e.lower() in src and e not in out:
            out.append(e)
    return out


def _is_isoish(value: object) -> bool:
    """A plausible ISO-8601 date-time string (shape check, not a full parse)."""
    return bool(_ISO_DT_RE.search(str(value or "")))


def _action_source(action: dict, brief: str = "") -> str:
    """The distilled text a typed spec's args may draw from — never the raw
    transcript, only fields already extracted into the artifact."""
    parts = [
        str(action.get("item") or ""),
        str(action.get("owner") or ""),
        str(action.get("deadline") or ""),
        brief or "",
    ]
    return "\n".join(p for p in parts if p)


def _sanitize_typed(typed: object, action: dict, brief: str = "") -> dict | None:
    """Validate + normalise one model/stub-proposed typed spec against its
    action, or None when it doesn't cleanly map. Enforces grounded recipients
    and ISO-shaped times so a hallucinated field can never reach the executor."""
    if not isinstance(typed, dict):
        return None
    t = str(typed.get("type") or "").strip()
    args = typed.get("args") if isinstance(typed.get("args"), dict) else {}
    source = _action_source(action, brief)
    if t == EMAIL_SEND:
        to = _grounded_emails(args.get("to"), source)
        if not to:
            return None  # no grounded recipient → never send
        subject = str(args.get("subject") or "").strip()[:200]
        body = str(args.get("body") or "").strip()[:4000]
        if not subject and not body:
            subject = str(action.get("item") or "").strip()[:200]
        return {"type": t, "args": {"to": to, "subject": subject, "body": body}}
    if t == CALENDAR_CREATE:
        title = str(
            args.get("title") or args.get("summary") or action.get("item") or ""
        ).strip()[:200]
        start = str(args.get("start") or "").strip()
        end = str(args.get("end") or "").strip()
        if not title or not _is_isoish(start) or not _is_isoish(end):
            return None
        spec_args = {"title": title, "start": start, "end": end}
        attendees = _grounded_emails(args.get("attendees"), source)
        if attendees:
            spec_args["attendees"] = attendees
        return {"type": t, "args": spec_args}
    if t == ASANA_CREATE:
        name = str(args.get("name") or action.get("item") or "").strip()[:200]
        if not name:
            return None
        spec_args: dict = {"name": name}
        notes = str(args.get("notes") or "").strip()[:1000]
        if notes:
            spec_args["notes"] = notes
        # Assignee: a grounded email only — a bare first name can't be safely
        # resolved to an Asana user, and inventing an assignee is worse than
        # creating the task unassigned.
        assignee = _grounded_emails(args.get("assignee"), source)
        if assignee:
            spec_args["assignee"] = assignee[0]
        # Due date: ISO-date-shaped only (the model normalises a stated
        # "Friday" using TODAY, same contract as calendar times).
        due = str(args.get("due_on") or "").strip()
        if _ISO_DATE_RE.fullmatch(due):
            spec_args["due_on"] = due
        # Project: only a name that literally appears in the source text —
        # a wrong project is a misfiled task in someone's real board.
        project = str(args.get("project") or "").strip()[:100]
        if project and project.lower() in source.lower():
            spec_args["project"] = project
        return {"type": t, "args": spec_args}
    return None


def _stub_type_actions(
    indexed: list[tuple[int, dict]], brief: str = "", *, allow_asana: bool = False
) -> dict[int, dict]:
    """Deterministic, key-free mapping for the offline demo + tests: map the
    obvious cases from literal values only (an email address / ISO datetimes
    that actually appear). Precision over recall — anything ambiguous is left
    untyped, exactly like the model path. With ``allow_asana``, every item
    that didn't map to email/calendar becomes an asana.create_task (the item
    text IS the task name, so it is grounded by construction)."""
    out: dict[int, dict] = {}
    for i, a in indexed:
        item = str(a.get("item") or "")
        source = _action_source(a, brief)
        if _EMAIL_INTENT_RE.search(item):
            emails = _grounded_emails(source, source)
            if emails:
                out[i] = _sanitize_typed(
                    {"type": EMAIL_SEND, "args": {"to": emails, "subject": item}},
                    a, brief,
                ) or out.get(i)
                if out.get(i):
                    continue
        if _CAL_INTENT_RE.search(item):
            dts = _ISO_DT_RE.findall(source)
            if len(dts) >= 2:
                spec = _sanitize_typed(
                    {
                        "type": CALENDAR_CREATE,
                        "args": {"title": item, "start": dts[0], "end": dts[1],
                                 "attendees": _grounded_emails(source, source)},
                    },
                    a, brief,
                )
                if spec:
                    out[i] = spec
                    continue
        if allow_asana and i not in out and item.strip():
            emails = _grounded_emails(source, source)
            dates = _ISO_DATE_RE.findall(source)
            spec = _sanitize_typed(
                {
                    "type": ASANA_CREATE,
                    "args": {
                        "name": item,
                        "assignee": emails[0] if emails else "",
                        "due_on": dates[0] if dates else "",
                    },
                },
                a, brief,
            )
            if spec:
                out[i] = spec
    return {k: v for k, v in out.items() if v}


def _llm_type_actions(
    indexed: list[tuple[int, dict]], brief: str, provider: str,
    *, allow_asana: bool = False,
) -> dict[int, dict]:
    """One post_provider() call to classify the actions; every returned spec is
    re-validated by _sanitize_typed (grounded recipients, ISO times) before it
    is trusted, so a hallucinated field can never survive."""
    import time as _time

    lines = []
    for i, a in indexed:
        owner = str(a.get("owner") or "").strip()
        deadline = str(a.get("deadline") or "").strip()
        suffix = (f" (owner: {owner})" if owner and owner.upper() != "UNASSIGNED" else "")
        suffix += f" (deadline: {deadline})" if deadline else ""
        lines.append(f"[{i}] {str(a.get('item') or '')}{suffix}")
    raw = llm.complete(
        TYPED_ACTION_SYSTEM + (TYPED_ACTION_ASANA if allow_asana else ""),
        (
            f"TODAY: {_time.strftime('%Y-%m-%d')}\n\n"
            + (f"Meeting summary (context only):\n{brief}\n\n" if brief.strip() else "")
            + "ACTION ITEMS (0-based index in brackets — the source text for each):\n"
            + "\n".join(lines)
            + "\n\nRespond with the JSON object only."
        ),
        max_tokens=1200,
        provider=provider,
    )
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        return {}
    by_idx = {i: a for i, a in indexed}
    out: dict[int, dict] = {}
    for key, spec in parsed.items():
        try:
            i = int(key)
        except (TypeError, ValueError):
            continue
        if i not in by_idx:
            continue
        clean = _sanitize_typed(spec, by_idx[i], brief)
        if clean:
            out[i] = clean
    return out


def type_actions(
    actions: list, brief: str = "", *, provider: str | None = None,
    allow_asana: bool = False,
) -> list:
    """Annotate each action with a ``typed`` spec where it clearly maps to a
    native-executor action (calendar.create_event / email.send, plus
    asana.create_task when ``allow_asana`` — set by finalize only for an
    avatar that MAY use the org's connected Asana).

    Returns a NEW list; an action that doesn't map is returned unchanged (no
    ``typed`` key). Never invents recipients or times — args draw only from that
    action's distilled fields + the meeting ``brief`` (never the raw
    transcript). Finalize-only, off the live path: it may make one
    post_provider() call (stub = a deterministic regex mapping, so the key-free
    demo still types the obvious cases). Best-effort: any failure returns the
    actions unchanged, so it can never break finalize."""
    src = list(actions or [])
    indexed = [(i, a) for i, a in enumerate(src) if isinstance(a, dict)]
    if not indexed:
        return src
    prov = (provider or post_provider()).lower()
    try:
        mapping = (
            _stub_type_actions(indexed, brief, allow_asana=allow_asana)
            if prov == "stub"
            else _llm_type_actions(indexed, brief, prov, allow_asana=allow_asana)
        )
    except Exception as e:  # noqa: BLE001 — enrichment only, never fatal
        print(f"[type_actions] skipped ({type(e).__name__})", flush=True)
        return src
    if not mapping:
        return src
    out: list = []
    for i, a in enumerate(src):
        if i in mapping:
            a = dict(a)
            a["typed"] = mapping[i]
        out.append(a)
    return out


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
    avatar: Avatar,
    transcript_text: str,
    state: "meeting_state.MeetingState",
    *,
    degraded: bool = False,
) -> dict:
    """Deterministic post-meeting artifact from simple transcript heuristics.

    `degraded=True` marks a recap built because the real post model returned an
    unusable result (rather than because we're in offline stub mode) — only the
    summary's mode note differs."""
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

    mode_note = (
        "auto-generated from the meeting tracker after the summary model "
        "returned an incomplete result"
        if degraded
        else "offline stub mode — enable a real brain for a true summary"
    )
    summary = (
        f"{avatar.name} sat in on a meeting with {len(speakers)} participant(s) "
        f"({', '.join(speakers) or 'unknown'}) across {len(lines)} lines. "
        f"{len(checklist)} potential action item(s)/process gap(s) were detected "
        f"by keyword heuristics ({mode_note})."
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
