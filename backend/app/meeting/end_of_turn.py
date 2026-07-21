"""End-of-turn estimation: does the last utterance sound FINISHED?

The single biggest turn-taking tell a fixed timer can't see: "I wanted to ask
about the..." and "What's the deadline?" both end in silence, but one speaker
is mid-thought and the other is waiting for an answer. Humans read this from
the words instantly; Laura should too.

`completeness(text)` returns 0..1; the likelihood the speaker has finished
their thought. It feeds `decision.adaptive_deference_seconds`, which SIZES the
deference wait (never the decision of whether to speak; the post-sleep yield
check and the in-stream SKIP gate stay in charge of that, so a wrong estimate
can only ever cost latency, never emit unaddressed speech).

v1 is a linguistic heuristic: zero network, zero model download, ~microseconds,
bilingual IT/EN. It reads these signals in risk-aware order:
  1. a trailing ellipsis holds the floor;
  2. a terminal question mark is the strongest "done" signal a transcript carries;
  3. trailing INCOMPLETENESS markers: a conjunction, preposition, article or
     filler ("about the", "e quindi", "with a"); otherwise hold the floor;
  4. remaining punctuation and length resolve the less certain cases.

v2 (the planned upgrade, do NOT bolt onto this file): pipecat smart-turn v3 -
an 8M-param audio model (BSD-2, ~12ms on CPU, Italian included) fed by Recall's
realtime per-participant audio. That needs a websocket RECEIVER, and App Runner
403s inbound WebSocket upgrades at the edge, so the receiver must live elsewhere
(the GPU pod or a small always-on worker) and forward end-of-turn probabilities
to this same seam. The interface here (text in, 0..1 out feeding the deference
sizer) is deliberately shaped so v2 swaps in without touching the callers.
Mid-word ASR truncation is intentionally deferred to that audio model: text alone
cannot safely distinguish a cutoff from a valid name, acronym, or domain term.
"""
from __future__ import annotations

import re
import unicodedata

# Words that essentially never END a finished thought (lowercase match on the
# final token, punctuation stripped). Bilingual: Laura works in IT and EN.
# Deliberately high-precision; a false "incomplete" only adds ~1s of wait.
_TRAILING_INCOMPLETE = {
    # EN: conjunctions / prepositions / articles / auxiliaries
    "and", "or", "but", "so", "because", "if", "then", "that", "which",
    "the", "a", "an", "of", "to", "for", "with", "about", "on", "in",
    "at", "by", "from", "is", "are", "was", "were", "has", "have", "had",
    "will", "would", "could", "should", "can", "very", "really", "quite",
    # IT: congiunzioni / preposizioni / articoli / ausiliari
    "e", "o", "ma", "però", "quindi", "perché", "se", "che", "cui",
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una",
    "di", "del", "della", "dello", "dei", "degli", "delle",
    "a", "al", "alla", "allo", "ai", "agli", "alle",
    "da", "dal", "dalla", "in", "nel", "nella", "con", "su", "sul", "sulla",
    "per", "tra", "fra", "è", "sono", "era", "erano", "ha", "hanno",
    "molto", "davvero", "abbastanza",
}

# Fillers that mark a held floor ("uhm", "allora..."); mid-thought pauses.
_TRAILING_FILLER = {
    "uh", "um", "uhm", "erm", "hmm", "like", "you know",
    "ehm", "cioè", "allora", "diciamo", "insomma", "tipo", "vediamo",
}


def _strip_trailing_symbol_run(text: str) -> str:
    """Remove trailing emoji/symbol clusters while preserving punctuation.

    Emoji clusters may include variation selectors, combining marks, and ZWJ
    format characters. Only trim the candidate run when it contains an actual
    Unicode symbol, so a decomposed accent on a final word is left untouched.
    """
    index = len(text)
    saw_symbol = False
    while index:
        char = text[index - 1]
        category = unicodedata.category(char)
        if char.isspace() or category[0] == "S" or category in {"Mn", "Me", "Cf"}:
            saw_symbol = saw_symbol or category[0] == "S"
            index -= 1
            continue
        break
    return text[:index].rstrip() if saw_symbol else text


def completeness(text: str) -> float:
    """0..1 likelihood that the speaker has finished their thought.

    Calibration anchors (used by the deference sizer's thresholds):
      >= 0.8  clearly finished (full question / punctuated sentence)
      ~  0.5  can't tell (unpunctuated but plausible clause)
      <= 0.3  clearly mid-thought (trailing connective/filler/fragment)
    """
    t = (text or "").strip()
    if not t:
        return 0.0

    # A trailing reaction must not hide the punctuation immediately before it:
    # "Great job! 🎉" has the same turn boundary as "Great job!".
    t = _strip_trailing_symbol_run(t)
    if not t:
        return 0.0

    # Trailing ellipsis is a spoken "...": the transcriber heard the trail-off.
    if t.endswith(("...", "…")):
        return 0.15

    # A complete question can naturally end in a word that is incomplete only
    # outside question syntax ("What did she mean by that?"). The question mark
    # is the stronger turn-yield signal, so check it before the final token.
    if t.endswith("?"):
        return 0.95

    last = re.sub(r"[^\w']+$", "", t).split()[-1].lower() if t.split() else ""
    words = len(t.split())

    if last in _TRAILING_FILLER:
        return 0.1
    if last in _TRAILING_INCOMPLETE:
        return 0.2

    if t.endswith("!"):
        return 0.85
    if t.endswith("."):
        # Punctuated, but a 1-2 word "sentence" is often a transcriber artifact
        # ("So." / "Allora."); treat short ones as weaker evidence.
        return 0.85 if words >= 3 else 0.6
    # No terminal punctuation (common in live ASR): length is the tiebreaker.
    if words <= 2:
        return 0.3
    if words <= 5:
        return 0.5
    return 0.6
