"""The when-to-speak gate.

MVP policy (intentionally conservative): the avatar speaks ONLY when called by
name. Proactive speech is a deliberate later step — see README "when-to-speak".

This module decides whether an utterance *addresses* a given avatar and, if so,
extracts the question to answer. All thresholds come from the avatar's config.
"""
from __future__ import annotations

import re

from .avatars import Avatar


def detect_wake(avatar: Avatar, utterance: str) -> tuple[bool, str]:
    """If the utterance calls the avatar by a wake word, return (True, question).

    Examples that trigger (wake word "laura"):
        "Laura, what are we missing?"   -> "what are we missing?"
        "Hey Laura what's the process"  -> "what's the process"
        "Can you check, Laura?"         -> "Can you check?"
    """
    lower = utterance.lower()
    for wake in avatar.wake_words:
        # Match the wake word as a standalone token.
        if re.search(rf"\b{re.escape(wake)}\b", lower):
            return True, _strip_wake(utterance, wake)
    return False, ""


def _strip_wake(utterance: str, wake: str) -> str:
    """Remove the wake word + filler ('hey', trailing/leading punctuation)."""
    cleaned = re.sub(rf"\b{re.escape(wake)}\b", "", utterance, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(hey|ok|okay|hi)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" ,.?!-—\t")
    return cleaned or utterance.strip()


def passes_confidence(avatar: Avatar, result: dict) -> bool:
    """Speak only if the model had enough grounded confidence for this avatar."""
    if not result.get("sufficient_context", False):
        return False
    return float(result.get("confidence", 0.0)) >= avatar.min_confidence


_CLOSING = re.compile(
    r"\b(wrap(ping)? up|that'?s (it|everything|all)|anything else|any other|"
    r"before we (go|close|end|wrap)|to summari[sz]e|let'?s (close|end|wrap)|"
    r"we'?re done|any final|last thing)\b",
    re.IGNORECASE,
)


def detect_closing(utterance: str) -> bool:
    """True if the utterance sounds like the meeting is wrapping up."""
    return bool(_CLOSING.search(utterance))


# Dismissal ("Laura, you can leave"). Only ever checked on the wake-stripped
# question of an utterance that addressed her BY NAME, so the patterns can stay
# tight. Two shapes: an imperative aimed at her at the start of the ask, or an
# explicit "you can/may …" permission anywhere in it. Deliberately narrow —
# a missed command costs a repeat ask; a false positive kills the meeting bot.
_LEAVE_IMPERATIVE = re.compile(
    # The imperative must be the WHOLE ask ("leave", "please leave the call
    # now") — anything else after the verb ("leave the pricing for later",
    # "leave it with me") means a topic, not the meeting.
    r"^(?:please\s+|now\s+|just\s+|kindly\s+|go ahead and\s+)*"
    r"(?:leave|exit|drop off|hop off|hang up|disconnect)"
    r"(?:\s+(?:the|this)\s+(?:meeting|call|room))?"
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
    r"\byou (?:"
    r"(?:can|may|should) (?:leave|drop off|hop off|head out"
    r"|go(?=\s+(?:now|home)\b|\s+(?:the|this)\s+(?:meeting|call|room)))"
    r"|are free to (?:leave|go|drop off|head out)"
    r")"
    r"(?:\s+(?:the|this)\s+(?:meeting|call|room))?"
    r"(?:\s+(?:now|home|please|thanks|thank you|if you want|whenever))*"
    r"\s*(?:[.!?,;]|$)",
    re.IGNORECASE,
)
# The whole ask is just a farewell ("Laura, bye!", "goodbye Laura").
_LEAVE_FAREWELL = re.compile(
    r"^(?:(?:good)?bye(?:\s*bye)?|ciao|see you(?: later| soon| next time)?|"
    r"thanks,?\s*(?:good)?bye)[.!\s]*$",
    re.IGNORECASE,
)
# Negation / hypothetical right before the verb ("don't leave", "before you
# leave the meeting…") — never a command.
_LEAVE_BLOCKED = re.compile(
    r"\b(?:don'?t|do not|never|shouldn'?t|won'?t|before|unless|until|if|when|"
    r"why(?: did| would)?|instead of)\b[^.?!]{0,24}\b(?:leave|go|drop|hop|exit)\b",
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
        or _LEAVE_FAREWELL.match(q)
    )
