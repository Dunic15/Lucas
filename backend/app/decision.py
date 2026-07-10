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
from typing import Iterable

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


# ── fuzzy name matching ──
# ASR mangles spoken names ("Laura" -> "Lara"/"Lora"/"Loura"); benchmarks show
# explicit-name cue response drops from ~94% to ~68% under phonetic corruption
# (docs/research/multiparty-meeting-intelligence.md). Guarded tightly: same
# first letter (or a phonetically-equivalent sibilant initial — STT hears the
# soft-C "Cedric" as "Sedric"/"Kedric"), similar length, and edit distance 1 —
# or distance 2 only when the consonant skeleton matches exactly ("lora"→"lr"
# == "laura"→"lr", while "libra"→"lbr" stays out, and "clara" still fails:
# 'c' and 'l' are not equivalent initials).
_VOWELS = set("aeiou")
# Real dictionary words that sit within fuzzy range of a wake word but are
# never a name. "laurea/lauree" (Italian: degree) is edit distance 1 from
# "laura" — without this, every graduation mention would wake her.
_FUZZY_EXCLUDE = {"laurea", "lauree", "lauro"}

# Soft-C / sibilant initials ASR confuses: spoken "Cedric" is transcribed
# "Sedric"/"Kedric"/"Zedric" (soft C ≈ /s/, hard C ≈ /k/). Treating c/s/k/z as
# one initial class lets those corruptions resolve WITHOUT opening the gate to
# unrelated names — the length + edit-distance checks still reject the rest, so
# "Cedric" never matches "Frederick", and non-sibilant names are unaffected
# ("clara" still fails against "laura": 'c' and 'l' aren't equivalent).
_SIBILANT_INITIALS = frozenset("cskz")


def _initials_equivalent(a: str, b: str) -> bool:
    """First letters equal, or both phonetically-equivalent sibilant initials."""
    return a == b or (a in _SIBILANT_INITIALS and b in _SIBILANT_INITIALS)


def _levenshtein(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 3  # caller only cares about <=2
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def fuzzy_name_match(token: str, name: str) -> bool:
    """True when `token` is (a close ASR corruption of) the spoken `name`."""
    token, name = token.lower(), name.lower()
    if token == name:
        return True
    if token in _FUZZY_EXCLUDE:
        return False
    if len(token) < 3 or len(name) < 4 or not _initials_equivalent(token[0], name[0]):
        return False
    d = _levenshtein(token, name)
    if d <= 1:
        return True
    if d == 2:
        skel = lambda s: "".join(c for c in s if c not in _VOWELS)  # noqa: E731
        return skel(token) == skel(name)
    return False


def _fuzzy_wake_token(lower: str, wake: str, excluded: set[str]) -> str:
    """The token in `lower` that fuzzy-matches `wake` ("" when none).

    `excluded` holds first names of OTHER people in the meeting: a token that
    IS someone's actual name ("Lara" when a Lara is on the call) is them being
    addressed, never a corruption of the avatar's name.
    """
    for token in re.findall(r"[a-z]+", lower):
        if token in excluded:
            continue
        if fuzzy_name_match(token, wake):
            return token
    return ""


def detect_wake(
    avatar: Avatar,
    utterance: str,
    exclude_names: Iterable[str] = (),
    fuzzy: bool = True,
) -> tuple[bool, str]:
    """If the utterance calls the avatar by a wake word, return (True, question).

    Examples that trigger (wake word "laura"):
        "Laura, what are we missing?"   -> "what are we missing?"
        "Hey Laura what's the process"  -> "what's the process"
        "Can you check, Laura?"         -> "Can you check?"
        "Lara, what's the next step?"   -> ASR-corrupted name still wakes

    Examples that do NOT trigger (the avatar is only being talked about):
        "as Laura said earlier, we should ship"
        "what did Laura mean by handoff?"
        "Laura mentioned the deadline"

    `exclude_names` (other meeting participants) suppresses only the FUZZY
    path: with a real Lara in the room, "Lara, …" is her turn — while an
    exact wake word always wins.
    """
    lower = utterance.lower()
    excluded = {
        n.strip().split()[0].lower() for n in exclude_names if n and n.strip()
    }
    for wake in avatar.wake_words:
        # Exact standalone token first; then a fuzzy ASR-corruption of it.
        # The matched TOKEN (not the canonical wake word) drives the reported-
        # speech check and the strip, since that's what's actually in the text.
        matched = (
            wake
            if re.search(rf"\b{re.escape(wake)}\b", lower)
            else (_fuzzy_wake_token(lower, wake, excluded) if fuzzy else "")
        )
        if matched:
            if _is_reported_reference(lower, matched):
                return False, ""
            return True, _strip_wake(utterance, matched)
    return False, ""


def _strip_wake(utterance: str, wake: str) -> str:
    """Remove the wake word + filler ('hey', trailing/leading punctuation)."""
    cleaned = re.sub(rf"\b{re.escape(wake)}\b", "", utterance, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(hey|ok|okay|hi)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" ,.?!-—\t")
    return cleaned or utterance.strip()


# Vocative labels that are never real spoken names (anonymous roster entries).
_NON_VOCATIVE = {"guest", "everyone", "all", "team", "folks", "guys", "ragazzi"}

# Live ASR frequently drops the comma in a leading vocative: "Marco can you
# confirm?". Accept that shape only when the name is followed by an unmistakable
# second-person question or imperative. This intentionally excludes ordinary
# subject mentions such as "Marco can confirm" and "Marco will own the rollout".
_NO_COMMA_VOCATIVE = re.compile(
    r"(?:^|[.;:!?]\s+)([a-z]+)\s+(?="
    r"(?:(?:can|could|would|will|do|did|are|were|have|has)\s+you\b|"
    r"(?:what|when|where|why|how)\b|"
    r"(?:please|tell|show|give|take|send|share|confirm|check|explain)\b|"
    r"(?:per favore|puoi|potresti|potete|vuoi|volete|riesci|riuscite|cosa)\b)"
    r")",
    re.IGNORECASE,
)


def addressed_to_other(utterance: str, roster: list[str]) -> bool:
    """True when the line is a vocative aimed at ANOTHER participant by name
    ("Marco, can you take this?", "hey Marco…", "…right, Marco?") — even in
    no-wake-word mode that's their turn, not the avatar's. Callers must check
    the wake word FIRST: a line naming both ("Laura, tell Marco…") is hers.

    Deliberately conservative: only a clear vocative counts. Merely mentioning
    a name mid-sentence ("Marco will own the rollout") is normal meeting talk
    the avatar may still answer about.
    """
    lower = (utterance or "").lower()
    if not lower:
        return False
    # Tokens sitting in a vocative position: "X, …" / "X can you …" /
    # "hey X …" / "…, X?".
    # Fuzzy-compared against roster first names so an ASR-mangled "Marko,
    # can you…" still reads as Marco's turn.
    candidates = set(
        re.findall(r"(?:^|[,.;:!?]\s+)([a-z]+)\s*[,:]", lower)
        + re.findall(r"\b(?:hey|hi|hello|ok|okay|yo|ehi|ciao|senti|scusa|allora)\s+([a-z]+)\b", lower)
        + re.findall(r",\s*([a-z]+)[^a-z]*$", lower)
        + _NO_COMMA_VOCATIVE.findall(lower)
    )
    if not candidates:
        return False
    for name in roster:
        first = (name or "").strip().split()[0].lower().rstrip(",.")
        if len(first) < 3 or first in _NON_VOCATIVE:
            continue
        if any(fuzzy_name_match(tok, first) for tok in candidates):
            return True
    return False


def adaptive_deference_seconds(
    base: float,
    *,
    enabled: bool,
    lo: float,
    hi: float,
    since_partial: float,
    active_partial_seconds: float,
    n_humans: int,
    is_question: bool,
    turn_completeness: float | None = None,
) -> float:
    """Size ONLY the deference wait — never the decision of WHETHER she speaks.

    The caller's post-sleep yield check (transcript grew OR a human partial
    landed) and the in-stream SKIP sentinel are unchanged, so a mis-sized wait
    can at worst change latency: it can never emit unaddressed or double speech.

    When ``enabled`` is False (the default) this returns ``base`` verbatim — a
    strict no-op reproducing the fixed ``deference_seconds`` behaviour. The
    ``0 < lo < hi`` guard makes a mis-configured range (a non-positive or
    inverted bound) fall back to ``base`` rather than shrink the yield window to
    zero — a ``lo`` of 0 would let the single-human branch sleep(0) and give a
    human no chance to take the floor, so it is rejected, not honoured.

    When enabled, the wait adapts to signals already on hand, strongest first:
      • the utterance sounds MID-THOUGHT (``turn_completeness`` low, from
        end_of_turn.completeness: trailing conjunction/filler/trail-off) →
        wait ``hi``: the speaker is still holding the floor, whatever the
        room's shape — this outranks every other signal;
      • a human partial is mid-utterance (``since_partial`` small) → wait ``hi``
        (someone is audibly talking right now);
      • only one human present → wait ``lo`` (respond snappily, no one to defer
        to) — trimmed further when their line sounds clearly FINISHED;
      • a room-open question with several humans → split the difference —
        trimmed toward ``lo`` when clearly finished (a fully-formed question
        deserves a snappy answer, someone is waiting for it);
      • otherwise (a statement) → the ``base`` wait.
    Result is always clamped to ``[lo, hi]``.
    """
    if not enabled or not (0 < lo < hi):
        return base
    clearly_done = turn_completeness is not None and turn_completeness >= 0.8
    if turn_completeness is not None and turn_completeness <= 0.3:
        wait = hi  # mid-thought: give the speaker room to finish
    elif since_partial < active_partial_seconds:
        wait = hi
    elif n_humans <= 1:
        wait = lo
    elif is_question:
        wait = lo if clearly_done else (base + lo) / 2.0
    else:
        wait = base
    return max(lo, min(hi, wait))


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


# ── action-capture continuation guard ──
# main.py keeps a short same-speaker window after a captured action so an ask
# that ASR split across two finals ("send the recap" + "to the whole team by
# Friday") lands on ONE action card. That window must NOT swallow a genuinely
# NEW sentence said right after the capture (live repro 2026-07-10: "manda un
# messaggio di prova a Ben su Slack…" + 3s later "perfetto, direi che abbiamo
# finito il test" → dirty card). A real ASR split picks up MID-PHRASE; a new
# thought opens with an acknowledgement/appreciation marker or sounds like the
# meeting wrapping up (detect_closing). Lexical only, O(1) — this sits on the
# live path. Openers that can plausibly start a real split continuation are
# deliberately NOT here ("right after lunch", "good before Friday").
_CAPTURE_BREAK_OPENERS = re.compile(
    r"^(?:ok(?:ay)?|alright|great|perfect|awesome|excellent|"
    r"thanks|thank you|got it|sounds good|"
    # Italian
    r"perfetto|ottimo|benissimo|va bene|bene|grazie|d'accordo|"
    r"capito|ricevuto|direi che)\b",
    re.IGNORECASE,
)


def is_capture_continuation(text: str) -> bool:
    """True when a same-speaker follow-up right after a captured action reads
    as the CONTINUATION of that ask (a real ASR split final), not a new one.

    "to the whole team by Friday"                 -> True  (glue onto the card)
    "perfetto, direi che abbiamo finito il test"  -> False (new sentence)

    Deliberately conservative in what it REJECTS: a wrongly-missed glue only
    truncates the card text (the finalize summarizer still sees the whole
    transcript), while a wrong glue dirties the approval card the owner acts on.
    """
    t = (text or "").strip()
    if not t:
        return False
    if _CAPTURE_BREAK_OPENERS.match(t):
        return False
    if detect_closing(t):
        return False
    # A dismissal can land immediately after the task ask (the normal product
    # flow is "send the recap" -> "you can leave").  Treating it as an ASR
    # continuation glues the dismissal onto the approval card and refreshes
    # last_capture, so repeated leave asks can be swallowed forever.  Let the
    # dedicated leave gate in main.py handle it instead.
    if detect_leave_command(t):
        return False
    return True


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
    # meeting. Non-native/ASR-noisy prepositions are all accepted ("go out
    # FROM the meeting", "go out the meeting").
    r"^(?:please\s+|now\s+|just\s+|kindly\s+|go ahead and\s+)*"
    r"(?:leave|exit|go out|get out|go away|drop (?:off|out)|hop off|hang up|disconnect|log (?:off|out)|sign (?:off|out))"
    r"(?:\s+(?:(?:of|from|off)\s+)?(?:the|this|our)\s+(?:meeting|meet|call|room))?"
    r"(?:\s+(?:now|please|thanks|thank you))*"
    r"[.!?\s]*$",
    re.IGNORECASE,
)
# The polite QUESTION form of the dismissal ("Laura, can you leave the
# meeting?"). Same guard as the imperative: the verb phrase must be the whole
# ask, so "can you leave the pricing for next week" / "could you go over the
# numbers" never match.
_LEAVE_REQUEST = re.compile(
    r"^(?:ok(?:ay)?\s+|so\s+|now\s+|please\s+)*"
    r"(?:can|could|would|will) you (?:please\s+)?"
    r"(?:leave|exit|go out|get out|go away|drop (?:off|out)|hop off|hang up|disconnect|log (?:off|out)|sign (?:off|out))"
    r"(?:\s+(?:(?:of|from|off)\s+)?(?:the|this|our)\s+(?:meeting|meet|call|room))?"
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
    r"(?:can|may|should) (?:leave|exit|go out|get out|drop (?:off|out)|hop off|head out|log (?:off|out)|sign (?:off|out)"
    r"|disconnect|hang up|go(?=\s+(?:now|home)\b|\s+(?:the|this)\s+(?:meeting|meet|call|room)))"
    r"|are free to (?:leave|go|drop (?:off|out)|head out)"
    r")"
    r"(?:\s+(?:(?:of|from|off)\s+)?(?:the|this|our)\s+(?:meeting|meet|call|room))?"
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
    r"^(?:per favore\s+|ora\s+|adesso\s+|pure\s+|ok\s+)*"
    # "lascia" only with the meeting as object ("lascia la riunione") — bare
    # "lascia pure/stare" means "never mind", not a dismissal.
    r"(?:esci(?:\s+fuori)?|vattene|vai via|vai fuori|scollegati|abbandona|vai pure|"
    r"lascia(?:ci)?(?=\s+(?:pure\s+)?(?:la|il|lo|questa|questo)\s+(?:riunione|call|chiamata|meeting|meet)))"
    r"(?:\s+pure)?"
    r"(?:\s+(?:dalla|dal|dallo|da (?:questa|questo|qui)|la|il|lo|questa|questo)\s+(?:riunione|call|chiamata|meeting|meet))?"
    r"(?:\s*,?\s*(?:ora|adesso|subito|pure|grazie|per favore|please))*[.!?\s]*$"
    r"|\b(?:puoi|potresti|potete)\s+(?:andare|andartene|uscire|lasciarci|abbandonare|scollegarti)"
    r"(?:\s+(?:dalla|dal|dallo|da (?:questa|questo|qui)|la|il|lo|questa|questo)\s+(?:riunione|call|chiamata|meeting|meet))?"
    r"(?:\s*,?\s*(?:ora|adesso|subito|pure|grazie|per favore|please))*\s*(?:[.!?,;]|$)"
    r"|\bte ne puoi andare\b|\bve ne potete andare\b"
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
        or _LEAVE_REQUEST.search(q)
        or _LEAVE_PERMISSION.search(q)
        or _LEAVE_IT.search(q)
        or _LEAVE_FAREWELL.match(q)
    )


# First tokens a dismissal aimed at THE AVATAR can start with: the leave verbs
# themselves, second-person pronouns, modals, and politeness/discourse lead-ins
# — everything the _LEAVE_* shapes actually accept. A follow-up that starts
# with anything else ("Sara you can leave now") is aimed at whoever was just
# named, never at the avatar. Used ONLY on the split-window follow-up path,
# where there is no wake word to disambiguate the addressee.
_FOLLOWUP_LEADS = frozenset(
    # EN verbs / pronouns / modals / leads
    "leave exit go get drop hop hang disconnect log sign you she can could "
    "would will please now just kindly ok okay so right thanks thank bye "
    "goodbye see well and but then anyway alright actually yes no".split()
    # IT verbs / pronouns / modals / leads. The discourse markers (dai, quindi,
    # bene, poi, comunque…) matter: live speech leads with them ("dai, esci
    # pure") and the 2026-07-10 test showed an armed window still not firing.
    + "esci vattene vai scollegati abbandona lascia lasciaci puoi potresti "
      "potete per ora adesso subito pure va sì si te ve sei ciao arrivederci "
      "grazie allora dai quindi bene poi e ma comunque senti ecco beh boh ah "
      "oh perfetto niente".split()
)


def plausible_leave_followup(text: str) -> bool:
    """Addressee guard for a split-final dismissal (no wake word in the line):
    True when the ask STARTS like a command aimed at the avatar. "you can
    leave now" leads with a pronoun → plausible; "Sara you can leave now"
    leads with a name → aimed at Sara (even if the roster doesn't know her),
    so the avatar must stay. A missed dismissal costs a repeat ask; a false
    positive kills the meeting bot — hence the whitelist direction."""
    m = re.search(r"[a-zà-ú]+", (text or "").lower())
    return bool(m) and m.group(0) in _FOLLOWUP_LEADS
