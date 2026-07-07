"""The when-to-speak gate.

Policy: in a live meeting the avatar speaks only when it is genuinely
*addressed* — called by name (a vocative), or asked a clear, on-topic process
question. Speech that merely *mentions* the avatar in the third person ("as Laura
said earlier", "what did Laura mean") must NOT wake it. Everything else prefers
silence. This module owns three pure-logic concerns the live path leans on:

  1. detect_wake       — is this utterance addressed to the avatar? (+ the ask)
  2. is_direct_question — an on-topic question worth answering unprompted?
  3. repetition guard   — has the avatar effectively already said/answered this?

All of it is regex/string logic — zero latency on the live path, no model call.
"""
from __future__ import annotations

import re

from .avatars import Avatar

# Third-person verbs that follow a wake word when someone is talking ABOUT the
# avatar, not TO it: "Laura said…", "Laura mentioned…", "Laura was saying…".
_REPORTED_TRAILING = (
    r"said|says|saying|mentioned|meant|means|told|thinks|thought|noted|"
    r"pointed|explained|suggested|asked|wanted|raised|flagged|had|was|were|'s"
)
# Subordinating / referential words that precede a wake word in reported speech:
# "as Laura…", "what did Laura…", "about Laura…", "according to Laura…".
_REPORTED_LEADING = (
    r"as|what|when|whatever|like|because|since|that|did|does|per|about|"
    r"regarding|according to|from|for|with"
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
        re.search(rf"\b(?:hey|hi|hello|ok|okay|yo)\s+{w}\b", lower)
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


# ─────────────────────── direct on-topic questions ────────────────────────
# Even without the wake word, a clear question about the avatar's actual domain
# (process / onboarding / security / SFF fund + portfolio) is worth answering.
# Everything else stays silent in live meetings. This keeps the avatar useful
# without turning it into an interjecting chatbot.
_DOMAIN_VOCAB = re.compile(
    r"\b(onboard\w*|process|policy|policies|sop|procedure|approv\w*|sign[- ]?off|"
    r"owner|assign\w*|deadline|hand[- ]?off|hand[- ]?over|go[- ]?live|provision\w*|"
    r"security|access|dpa|data processing|compliance|audit|"
    r"portfolio|fund|thesis|invest\w*|startup|founder|mentor\w*|bootcamp|"
    r"exit\w*|sff|company|companies|sector)\b",
    re.IGNORECASE,
)


def is_direct_question(avatar: Avatar, utterance: str) -> bool:
    """True if this reads like a clear, on-topic question the avatar should answer
    even though it wasn't called by name — but NOT if it's only reported speech."""
    text = (utterance or "").strip()
    if "?" not in text:
        return False
    lower = text.lower()
    for wake in avatar.wake_words:
        if re.search(rf"\b{re.escape(wake)}\b", lower) and _is_reported_reference(
            lower, wake
        ):
            return False
    return bool(_DOMAIN_VOCAB.search(text))


# ───────────────────────────── repetition guard ───────────────────────────
# In-memory, per-meeting: don't let the avatar repeat itself. We compare
# normalized token sets (Jaccard) so light rephrasings still count as the same
# thing. No transcript text is persisted — the gists live only on the Session.
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "the a an of to and or is are was were be been do does did we you i it that "
    "this for on in at by with as what who when how our your my me us they them "
    "so about per from can could should would will".split()
)


def normalize_text(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def _content_tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall((text or "").lower()) if t not in _STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of content tokens; 1.0 == same content words."""
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_near_duplicate(text: str, priors: list[str], *, threshold: float = 0.7) -> bool:
    return any(similarity(text, p) >= threshold for p in priors)
