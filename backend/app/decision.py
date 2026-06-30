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

    Examples that trigger (wake word "sofia"):
        "Sofia, what are we missing?"   -> "what are we missing?"
        "Hey Sofia what's the process"  -> "what's the process"
        "Can you check, Sofia?"         -> "Can you check?"
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
