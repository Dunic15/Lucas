"""ASR text normalization — pure, dependency-free helpers.

Speech-to-text renders dictated email addresses as words ("duccio at sff
studio dot com"), which made spoken addresses invisible to everything that
matches real addresses (the contacts lookup, the typed-action email grounding
guard). ``normalize_spoken_email`` rewrites ONLY spans that convert into a
well-formed address; everything else passes through byte-identical, so prose
that merely contains the word "at" is never mangled.
"""
from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# "<user> at <domain words> dot <tld>". The user part is ONE token — allowing
# spaces there glued leading verbs in ("email duccio at …" -> emailduccio@).
# The domain group is GREEDY so it runs to the LAST "dot" ("sff dot studio dot
# com" -> sff.studio.com); spoken "dot" inside it becomes "." and spaces
# collapse ("sff studio" -> sffstudio). The rewrite is kept ONLY if the
# assembled string matches _EMAIL_RE.
# The domain words may include spoken "dot" but never the word "at" — that
# guard is what anchors the match to the LAST "at" in lines like "the summary
# at duccio at sff studio dot com" (the user part must sit right before it).
_SPOKEN_EMAIL_RE = re.compile(
    r"\b([a-z0-9][a-z0-9._-]{0,40})\s+at\s+"
    r"((?!at\b)[a-z0-9._-]+(?:\s+(?!at\b)[a-z0-9._-]+)*)"
    r"\s+dot\s+([a-z]{2,10})\b",
    re.IGNORECASE,
)

# Ordinary-English tokens that precede a literal "at" all the time ("meet me
# at cafe dot com", "I was at home dot com"). A user part in this set is
# prose, not an address — never rewritten. Real given names colliding with
# these are vanishingly rare.
_USER_STOPWORDS = frozenset(
    "i me we us you he she it they them was were is are am be been being "
    "meet met meets meeting stay stayed staying look looked looking arrive "
    "arrived up out back here there home work again still not now".split()
)


def _assemble(user: str, domain: str, tld: str) -> str:
    """Join spoken fragments into a candidate address: spoken "dot" inside the
    domain becomes "." and remaining spaces collapse."""
    d = re.sub(r"\s+dot\s+", ".", domain.strip(), flags=re.IGNORECASE)
    d = re.sub(r"\s+", "", d)
    return f"{user.strip()}@{d}.{tld.lower()}"


def normalize_spoken_email(text: str) -> str:
    """Rewrite spoken-address spans ("x at y dot com") into x@y.com.

    Only a span whose assembled result matches a real-address shape is
    rewritten; anything else is left exactly as it was. Idempotent on text
    that already contains real addresses."""
    if not text or " at " not in f" {text.lower()} ":
        return text

    def _sub(m: re.Match) -> str:
        if m.group(1).lower() in _USER_STOPWORDS:
            return m.group(0)
        candidate = _assemble(m.group(1), m.group(2), m.group(3))
        return candidate if _EMAIL_RE.fullmatch(candidate) else m.group(0)

    return _SPOKEN_EMAIL_RE.sub(_sub, text)
