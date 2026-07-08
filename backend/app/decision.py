"""The when-to-speak gate — wake detection, closing detection, dismissal.

Laura tracks the WHOLE meeting silently (see meeting_state.py) no matter what.
The wake word is optional and only gates *speaking*: with REQUIRE_WAKE_WORD
false (the default) she answers any groundable question without her name, and
the in-stream SKIP sentinel (brain.answer_question_stream) is what actually
enforces grounding. `detect_wake` here just recognises when she's addressed by
name (so a direct question always gets an answer) and separates a vocative
("Laura, …") from a third-person mention ("as Laura said").

This module also detects meeting close (for the proactive wrap-up) and the
"you can leave" dismissal. Thresholds come from the avatar's config.
"""
from __future__ import annotations

import re

from .avatars import Avatar

# Third-person verbs that follow a wake word when someone is talking ABOUT the
# avatar, not TO it: "Laura said…", "Laura mentioned…", "Laura ha detto…".
_REPORTED_TRAILING = (
    r"said|says|saying|mentioned|meant|means|told|thinks|thought|noted|"
    r"pointed|explained|suggested|asked|wanted|raised|flagged|had|was|were|'s|"
    # Italian: "Laura ha detto…", "Laura diceva…", "Laura intendeva…"
    r"ha|aveva|dice|diceva|intende|intendeva|pensa|pensava|sosteneva|suggeriva"
)
# Subordinating / referential words that precede a wake word in reported speech:
# "as Laura…", "what did Laura…", "secondo Laura…", "come diceva Laura…".
_REPORTED_LEADING = (
    r"as|what|when|whatever|like|because|since|that|did|does|per|about|"
    r"regarding|according to|from|for|with|"
    r"secondo|come (?:ha detto|diceva|dice)|quello che|cosa (?:ha detto|diceva)|di"
)


def _is_reported_reference(lower: str, wake: str) -> bool:
    """True when the wake word appears ONLY as a third-person reference to the
    avatar (talking about it), with no vocative signal that it's being addressed.

    "Laura mentioned the deadline"      -> reported (do not wake)
    "as Laura said earlier, we should"  -> reported (do not wake)
    "Laura, what are we missing?"        -> vocative (wake)
    "Laura, what did Laura mean?"        -> vocative wins (wake)
    """
    w = re.escape(wake)
    reported = bool(
        re.search(rf"\b{w}\s+(?:{_REPORTED_TRAILING})\b", lower)
        or re.search(rf"\b(?:{_REPORTED_LEADING})\s+{w}\b", lower)
    )
    if not reported:
        return False
    # A vocative signal means the speaker is addressing the avatar directly, which
    # overrides an incidental third-person mention elsewhere in the same line.
    vocative = bool(
        re.search(rf"\b(?:hey|hi|hello|ok|okay|yo|ehi|ciao|senti|scusa)\s+{w}\b", lower)
        or re.search(rf"(?:^|[,.;:!?]\s*){w}\s*[,:]", lower)   # "Laura, …" / "Laura:"
        or re.search(rf",\s*{w}\b[^a-z]*$", lower)             # "…, Laura?" (end)
    )
    return not vocative


def detect_wake(avatar: Avatar, utterance: str) -> tuple[bool, str]:
    """If the utterance calls the avatar by a wake word, return (True, question).

    Examples that trigger (wake word "laura"):
        "Laura, what are we missing?"   -> "what are we missing?"
        "Hey Laura what's the process"  -> "what's the process"
        "Can you check, Laura?"         -> "Can you check?"

    Examples that do NOT trigger (the avatar is only being talked about):
        "as Laura said earlier, we should ship"
        "what did Laura mean by handoff?"
        "Laura mentioned the deadline"
    """
    lower = utterance.lower()
    for wake in avatar.wake_words:
        # Match the wake word as a standalone token.
        if re.search(rf"\b{re.escape(wake)}\b", lower):
            if _is_reported_reference(lower, wake):
                return False, ""
            return True, _strip_wake(utterance, wake)
    return False, ""


def _strip_wake(utterance: str, wake: str) -> str:
    """Remove the wake word + filler ('hey', trailing/leading punctuation)."""
    cleaned = re.sub(rf"\b{re.escape(wake)}\b", "", utterance, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(hey|ok|okay|hi)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" ,.?!-—\t")
    return cleaned or utterance.strip()


def passes_confidence(avatar: Avatar, result: dict) -> bool:
    """LEGACY — not used on the live streaming path (the in-stream SKIP sentinel
    replaced it). Kept for back-compat + unit tests. Speak only if the model had
    enough grounded confidence for this avatar."""
    if not result.get("sufficient_context", False):
        return False
    return float(result.get("confidence", 0.0)) >= avatar.min_confidence


_CLOSING = re.compile(
    r"\b(wrap(ping)? up|that'?s (it|everything|all)|anything else|any other|"
    r"before we (go|close|end|wrap)|to summari[sz]e|let'?s (close|end|wrap)|"
    r"we'?re done|any final|last thing|"
    # Italian — without these the proactive wrap-up never fires in an Italian
    # meeting (and the MeetingState stage never reaches "wrapping_up").
    r"per riassumere|riassumendo|prima di (chiudere|concludere|salutarci)|"
    r"direi che (abbiamo finito|è tutto)|abbiamo finito|è tutto per oggi|"
    r"qualcos'?altro|altro da (aggiungere|dire|discutere)|"
    r"chiudiamo|concludiamo|ci (aggiorniamo|sentiamo|risentiamo|vediamo)|"
    r"un'?ultima cosa|per concludere|tiriamo le somme)\b",
    re.IGNORECASE,
)


def detect_closing(utterance: str) -> bool:
    """True if the utterance sounds like the meeting is wrapping up."""
    return bool(_CLOSING.search(utterance))


# ── stop command ("Laura, stop / aspetta / basta") ──
# Only checked on the wake-stripped ask of an utterance addressed BY NAME, so
# it can stay strict: the WHOLE ask must be stop vocabulary (+ politeness).
# "Laura, stop the deploy" is a request, not a stop; "Laura, aspetta" is a stop.
# This is the short-command complement to barge-in, which needs 3+ words.
_STOP_WORDS = (
    r"stop|wait|pause|hold on|hang on|shut up|be quiet|quiet|silence|enough|"
    r"one (?:sec|second|moment|minute)|give me a (?:sec|second|moment|minute)|"
    r"never ?mind|forget it|stop talking|that's enough|"
    # Italian
    r"aspetta|fermati|ferma|zitta|silenzio|basta|taci|un attimo|un secondo|"
    r"un momento|lascia (?:stare|perdere)|non importa|smettila|basta così"
)
_STOP_COMMAND = re.compile(
    rf"^(?:ok(?:ay)?\s+|no\s+|hey\s+|ehi\s+|per favore\s+|please\s+|just\s+)*"
    rf"(?:{_STOP_WORDS})"
    rf"(?:\s+(?:please|per favore|grazie|thanks|now|ora|adesso|a moment|un attimo))*"
    rf"[.!?\s]*$",
    re.IGNORECASE,
)


def detect_stop_command(question: str) -> bool:
    """True if the (wake-stripped) ask tells the avatar to stop talking NOW."""
    q = (question or "").strip()
    return bool(q) and bool(_STOP_COMMAND.match(q))


# Dismissal ("Laura, you can leave"). Only ever checked on the wake-stripped
# question of an utterance that addressed her BY NAME, so the patterns can stay
# tight. Two shapes: an imperative aimed at her at the start of the ask, or an
# explicit "you can/may …" permission anywhere in it. Deliberately narrow —
# a missed command costs a repeat ask; a false positive kills the meeting bot.
_LEAVE_IMPERATIVE = re.compile(
    # The imperative must be the WHOLE ask ("leave", "please leave the call
    # now", "go out of the meeting") — anything else after the verb ("leave
    # the pricing for later", "go out and check X") means a topic, not the
    # meeting.
    r"^(?:please\s+|now\s+|just\s+|kindly\s+|go ahead and\s+)*"
    r"(?:leave|exit|go out|get out|go away|drop off|hop off|hang up|disconnect)"
    r"(?:\s+(?:of\s+)?(?:the|this)\s+(?:meeting|call|room))?"
    r"(?:\s+(?:now|please|thanks|thank you))*"
    r"[.!?\s]*$",
    re.IGNORECASE,
)
_LEAVE_PERMISSION = re.compile(
    # "you can leave [the meeting] [now]" — the verb must end the clause, so
    # "you can leave time for Q&A" / "you can go to the next slide" never match.
    # Bare "go" is how a host hands over the floor ("your turn — you can go"),
    # i.e. an invitation to SPEAK, so "go" only counts with an explicit
    # dismissal marker after it; "free to go" is unambiguous on its own.
    r"\b(?:you|she) (?:"
    r"(?:can|may|should) (?:leave|drop off|hop off|head out|log off|sign off"
    r"|disconnect|hang up|go(?=\s+(?:now|home)\b|\s+(?:the|this)\s+(?:meeting|call|room)))"
    r"|are free to (?:leave|go|drop off|head out)"
    r")"
    r"(?:\s+(?:of\s+)?(?:the|this)\s+(?:meeting|call|room))?"
    r"(?:\s+(?:now|home|please|thanks|thank you|if you want|whenever))*"
    r"\s*(?:[.!?,;]|$)"
    # "we don't need you anymore" / "we're all set, thanks Laura"
    r"|\bwe (?:don'?t|no longer) need you\b"
    r"|\bnon (?:ci|ti) (?:servi|serve) più\b"
    r"|\bnon abbiamo più bisogno di te\b",
    re.IGNORECASE,
)
# The whole ask is just a farewell ("Laura, bye!", "goodbye Laura").
# NOTE: bare "ciao" is deliberately NOT here — in Italian it's also a GREETING
# ("Laura, ciao!" at the start of a meeting must never make her leave).
# "ciao ciao" and "arrivederci" are unambiguous farewells.
_LEAVE_FAREWELL = re.compile(
    r"^(?:(?:good)?bye(?:\s*bye)?|ciao\s+ciao|arrivederci|see you(?: later| soon| next time)?|"
    r"thanks,?\s*(?:good)?bye|grazie,?\s*ciao\s*ciao)[.!\s]*$",
    re.IGNORECASE,
)
# Italian dismissals, mirroring the English shapes: a whole-ask imperative
# ("esci pure", "lascia la riunione") or an explicit permission ("puoi
# andare"). Same guard as English: the verb must END the clause, so "puoi
# andare avanti" (= go ahead / continue) never matches.
_LEAVE_IT = re.compile(
    r"^(?:per favore\s+|ora\s+|adesso\s+|pure\s+)*"
    r"(?:esci|vattene|vai via|scollegati|abbandona|vai pure)"
    r"(?:\s+(?:dalla|da questa|la|questa)\s+(?:riunione|call|chiamata|meeting))?"
    r"(?:\s+(?:ora|adesso|pure|grazie))*[.!?\s]*$"
    r"|\bpuoi\s+(?:andare|uscire|lasciarci|abbandonare|scollegarti)"
    r"(?:\s+(?:dalla|da questa|la|questa)\s+(?:riunione|call|chiamata|meeting))?"
    r"(?:\s+(?:ora|adesso|pure|grazie))*\s*(?:[.!?,;]|$)"
    r"|\bsei liber[ao] di andare\b",
    re.IGNORECASE,
)
# Negation / hypothetical right before the verb ("don't leave", "before you
# leave the meeting…", "non andare", "prima di uscire…") — never a command.
_LEAVE_BLOCKED = re.compile(
    r"\b(?:don'?t|do not|never|shouldn'?t|won'?t|before|unless|until|if|when|"
    r"why(?: did| would)?|instead of|non|prima di|se|quando|perch[eé])\b"
    r"[^.?!]{0,24}\b(?:leave|go|drop|hop|exit|andare|uscire|esci|vattene|lasciare)\b",
    re.IGNORECASE,
)


def detect_leave_command(question: str) -> bool:
    """True if the (wake-stripped) ask tells the avatar to leave the meeting."""
    q = (question or "").strip()
    if not q:
        return False
    if _LEAVE_BLOCKED.search(q):
        return False
    return bool(
        _LEAVE_IMPERATIVE.search(q)
        or _LEAVE_PERMISSION.search(q)
        or _LEAVE_IT.search(q)
        or _LEAVE_FAREWELL.match(q)
    )
