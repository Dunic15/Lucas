"""Per-sentence emotion for the avatar's face and voice.

A spoken line carries an emotion so the renderer can move more than the mouth:
the 3D face (TalkingHead `setMood`) and the photoreal face (Ditto's `emo`
parameter, 0-7) both take a mood, and ElevenLabs voice settings can lean with
it. Today the face is emotionally flat — every line, good news or bad, wears the
same neutral expression. That flatness is the single biggest "it's a bot" tell.

Design choices:
- **Pure function, zero latency.** `classify(text)` is a keyword+punctuation
  heuristic — no LLM call on the live path, no network, no parse risk. The brain
  MAY override per line (it already emits JSON), but the default never blocks.
- **Small, honest label set.** Five moods that a talking-head can actually show
  and that map cleanly onto both renderers. More granularity than the model can
  express is wasted and reads as noise.
- **Neutral is the floor.** Ambiguous text stays neutral — a meeting avatar that
  emotes at random is worse than one that stays composed. We only leave neutral
  on a clear signal.
- **Contract-additive.** The label rides the existing {type:"speak"} message as
  one extra field; renderers that don't know it ignore it (see main.py).
"""
from __future__ import annotations

# The five moods, and how each maps onto the two faces.
#   talk_mood  -> TalkingHead setMood() name (its built-in mood set)
#   ditto_emo  -> Ditto `emo` index (0-7); 4 is the model's neutral default.
# Ditto's emotion indices follow the AffectNet-style order its training used:
# 0 neutral, 1 happy, 2 sad, 3 surprised, 4 (its documented neutral default),
# 5 angry, 6 disgust, 7 fear. We only use the expressive ones a business avatar
# should ever show; the rest stay mapped to neutral.
_MOODS: dict[str, dict] = {
    "neutral":   {"talk_mood": "neutral", "ditto_emo": 4},
    "happy":     {"talk_mood": "happy",   "ditto_emo": 1},
    "excited":   {"talk_mood": "happy",   "ditto_emo": 1},
    "serious":   {"talk_mood": "neutral", "ditto_emo": 0},
    "concerned": {"talk_mood": "sad",     "ditto_emo": 2},
}

DEFAULT = "neutral"

# Keyword signals. Lowercased substring match on word-ish boundaries. Bilingual
# (IT/EN) because Laura works in both. Kept deliberately small and high-precision
# — a false "happy" on a serious line is worse than a missed one.
_HAPPY = (
    "congratulations", "congrats", "great job", "well done", "excellent",
    "fantastic", "wonderful", "love it", "perfect", "awesome",
    "complimenti", "ottimo lavoro", "benissimo", "perfetto", "fantastico",
    "che bello", "meraviglioso", "evviva",
)
_CONCERNED = (
    "unfortunately", "i'm afraid", "problem", "issue", "risk", "concern",
    "blocked", "delay", "behind schedule", "missing", "at risk", "won't",
    "cannot", "failed", "overdue",
    "purtroppo", "temo che", "problema", "rischio", "ritardo", "bloccato",
    "manca", "in ritardo", "non riusciamo", "fallito", "scaduto",
)
_SERIOUS = (
    "important", "critical", "must", "deadline", "compliance", "legal",
    "required", "mandatory", "urgent",
    "importante", "critico", "scadenza", "obbligatorio", "urgente",
    "necessario", "conformità",
)


def _has(text: str, needles) -> bool:
    return any(n in text for n in needles)


def classify(text: str) -> str:
    """Map a line to one of the five moods. Neutral unless a clear signal fires.

    Order matters: concern/bad-news outranks excitement (a line that praises one
    thing and flags a risk should read as concerned, not happy), and an
    exclamation only turns a line happy when nothing negative is present.
    """
    if not text:
        return DEFAULT
    t = text.lower()

    if _has(t, _CONCERNED):
        return "concerned"
    if _has(t, _HAPPY):
        # "!" pushes praise to excited; plain praise stays happy.
        return "excited" if "!" in text else "happy"
    if _has(t, _SERIOUS):
        return "serious"
    # A bare exclamation with positive-ish framing and no risk word: mild lift.
    if text.count("!") >= 1 and "?" not in text:
        return "happy"
    return DEFAULT


def normalize(label: str | None) -> str:
    """Coerce any emotion string (e.g. from the brain's JSON) to a known mood."""
    if not label:
        return DEFAULT
    key = str(label).strip().lower()
    return key if key in _MOODS else DEFAULT


def talk_mood(label: str | None) -> str:
    """TalkingHead setMood() name for this emotion (3D face)."""
    return _MOODS[normalize(label)]["talk_mood"]


def ditto_emo(label: str | None) -> int:
    """Ditto `emo` index (0-7) for this emotion (photoreal face)."""
    return _MOODS[normalize(label)]["ditto_emo"]
