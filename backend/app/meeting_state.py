"""MeetingState — the silent intelligence layer.

Laura's product value in a meeting is knowing what the meeting has actually
covered: which required process steps happened, what was decided, who owns
what, what's at risk. This module folds every transcript line into a
structured MeetingState *silently* — no model call, pure regex heuristics —
so the live path pays zero added latency. The state is then used to decide
answer / silence / proactive warning, and to enrich the post-meeting artifact.

Process expectations come from per-avatar templates:
    avatars/<id>/process_templates/<template>.yaml
        id, name, required_steps: [...], critical_gaps: [...]

PII note: the state holds transcript-derived snippets in memory (like the
Session transcript itself). It is never logged and never persisted outside
the artifact store.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import yaml

from .avatars import Avatar
from .decision import detect_closing


# ────────────────────────── the state object ──────────────────────────
@dataclass
class MeetingState:
    meeting_type: str = ""            # template id, once detected ("" = unknown)
    stage: str = "start"              # start -> in_progress -> wrapping_up
    required_steps: list[str] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    missing_steps: list[str] = field(default_factory=list)
    critical_gaps: list[str] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    owners: list[dict] = field(default_factory=list)
    deadlines: list[dict] = field(default_factory=list)
    risks: list[dict] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    # Per-person view of the meeting: name -> {"lines", "commitments",
    # "questions", "risks"}. Powers "what did Marco commit to?" live and the
    # by-owner action list in the artifact. Avatar's own lines are excluded.
    per_person: dict[str, dict] = field(default_factory=dict)
    should_intervene: bool = False
    intervention_reason: str = ""

    def missing_critical(self) -> list[str]:
        return [s for s in self.missing_steps if s in self.critical_gaps]

    def readiness_score(self) -> int:
        """0-100: how many required process steps the meeting actually covered."""
        if not self.required_steps:
            return 0
        return round(100 * len(self.completed_steps) / len(self.required_steps))

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_type": self.meeting_type,
            "stage": self.stage,
            "required_steps": list(self.required_steps),
            "completed_steps": list(self.completed_steps),
            "missing_steps": list(self.missing_steps),
            "decisions": list(self.decisions),
            "owners": list(self.owners),
            "deadlines": list(self.deadlines),
            "risks": list(self.risks),
            "open_questions": list(self.open_questions),
            "per_person": {k: dict(v) for k, v in self.per_person.items()},
            "should_intervene": self.should_intervene,
            "intervention_reason": self.intervention_reason,
        }


# ───────────────────────── process templates ──────────────────────────
@dataclass(frozen=True)
class ProcessTemplate:
    id: str
    name: str
    required_steps: tuple[str, ...]
    critical_gaps: tuple[str, ...]


_template_cache: dict[str, list[ProcessTemplate]] = {}


def templates_for(avatar: Avatar) -> list[ProcessTemplate]:
    cached = _template_cache.get(avatar.id)
    if cached is not None:
        return cached
    templates: list[ProcessTemplate] = []
    folder = avatar.dir / "process_templates"
    if folder.exists():
        for path in sorted(folder.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text()) or {}
            templates.append(
                ProcessTemplate(
                    id=str(raw.get("id", path.stem)),
                    name=str(raw.get("name", path.stem)),
                    required_steps=tuple(raw.get("required_steps") or ()),
                    critical_gaps=tuple(raw.get("critical_gaps") or ()),
                )
            )
    _template_cache[avatar.id] = templates
    return templates


# What kind of meeting is this? First line that matches a template's hint
# locks the type in. Templates without a curated hint fall back to matching
# their human name in the conversation.
_TYPE_HINTS: dict[str, re.Pattern] = {
    "customer_onboarding": re.compile(
        r"\bonboard\w*|\bgo[- ]?live\b|\bprovision\w*|\bkick[- ]?off\b|"
        r"\bnew (customer|client)\b|\bimplementation (plan|timeline)\b",
        re.IGNORECASE,
    ),
    "implementation_access": re.compile(
        r"\b(grant|granting|get|getting|set up|setting up|need)\b[^.?!]*\baccess\b|"
        r"\bcredential\w*|\btechnical (owner|lead|contact|poc)\b|"
        r"\b(sandbox|staging) (environment|access|setup)\b|"
        r"\benvironment (access|setup|set-up)\b",
        re.IGNORECASE,
    ),
    "decision_quality": re.compile(
        r"\bdecision (meeting|review|time)\b|\bmake (a|the|our) (final )?decision\b|"
        r"\bdecide (on|between|which)\b|\bwhich (option|approach|vendor|path|direction)\b|"
        r"\bweigh\w* (the )?(options|trade)\b",
        re.IGNORECASE,
    ),
    "meeting_readiness": re.compile(
        r"\bmeeting readiness\b|\breadiness\b|\bready for (the|our|next|this|tomorrow)\b|"
        r"\bagenda for\b|\bpre[- ]?read\b|\bprep\w* for (the|our|next|this|tomorrow)\b",
        re.IGNORECASE,
    ),
}


def _type_hint(template: ProcessTemplate) -> re.Pattern:
    hint = _TYPE_HINTS.get(template.id)
    if hint is not None:
        return hint
    words = [re.escape(w) for w in template.name.split() if w]
    return re.compile(r"\W+".join(words), re.IGNORECASE)


# ─────────────────── per-line extraction heuristics ───────────────────
_DONE = re.compile(
    r"\b(approved|approves|signed(\s+off)?|sign[- ]off|confirmed|cleared|done|"
    r"completed|complete|finished|finalized|in place|all set|good to go|"
    r"green[- ]?light\w*|received|sorted|granted|provisioned)\b",
    re.IGNORECASE,
)
# "It's ready / set up / stood up" — completion phrasing for things that get
# stood up rather than approved (an environment, access).
_READY = re.compile(
    r"\b(ready|set up|set-up|spun up|stood up|provisioned|available|live|in place)\b",
    re.IGNORECASE,
)
_PENDING = re.compile(
    r"\b(need|needs|needed|still|waiting|pending|haven[’']?t|hasn[’']?t|"
    r"not yet|missing|blocked|blocker|unassigned|outstanding|open item|"
    r"to[- ]?do|follow[- ]?up)\b",
    re.IGNORECASE,
)
_SCHEDULED = re.compile(
    r"\b(scheduled|set for|booked|planned for|locked in|on the calendar|"
    r"targeting)\b",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|"
    r"\b\d{1,2}(st|nd|rd|th)?\s+(of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|"
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"\bnext (week|month|quarter)\b|\btomorrow\b|"
    r"\b(eod|eow|eom)\b|\bend of (day|week|month|quarter)\b|\bq[1-4]\b|"
    r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b",
    re.IGNORECASE,
)
_DEADLINE_CUE = re.compile(r"\b(by|due|deadline|no later than|before)\b", re.IGNORECASE)
_DECISION = re.compile(
    r"\b(we[’']?ve decided|we decided|we agreed|agreed to|the decision is|"
    r"let[’']?s go with|we[’']?ll go with|going with|final decision|"
    r"it[’']?s decided|we[’']?re going to go with)\b",
    re.IGNORECASE,
)
_RISK = re.compile(
    r"\b(risk\w*|concern\w*|worried|worry|blocker|blocked|at risk|red flag|"
    r"slip\w*|delay\w*|jeopard\w*)\b",
    re.IGNORECASE,
)
_OWNER_PATTERNS = [
    re.compile(r"\b([A-Z][a-z]+)\s+(?:will|is going to|can|should)\s+(?:own|lead|take|handle|drive|be responsible)\b"),
    re.compile(r"\bassign(?:ed|ing)?\s+(?:it\s+|this\s+|that\s+)?to\s+([A-Z][a-z]+)\b"),
    re.compile(r"\b([A-Z][a-z]+)\s+owns\b"),
    re.compile(r"\b([A-Z][a-z]+)\s+is\s+(?:the\s+)?owner\b"),
    # "Priya will be the technical owner" / "Sam is the decision owner / lead / POC"
    re.compile(
        r"\b([A-Z][a-z]+)\s+(?:will\s+be|is|is\s+going\s+to\s+be)\s+(?:the\s+)?"
        r"(?:technical\s+|decision\s+)?(?:owner|lead|poc|contact)\b"
    ),
]
_SELF_OWNER = re.compile(
    r"\bI[’']?ll\s+(?:own|take|handle|lead|drive)\b|"
    r"\bI\s+(?:will|can)\s+(?:own|take|handle|lead|drive)\b",
    re.IGNORECASE,
)
_PROCESS_QUESTION = re.compile(
    r"\b(who|when|owner|own\w*|deadline|approv\w*|sign[- ]?off|dpa|security|"
    r"hand[- ]?off|go[- ]?live|date)\b",
    re.IGNORECASE,
)

# Step topic vocabulary for the known template steps. Unknown step ids fall
# back to matching their own words ("customer_handoff" -> customer\W+handoff).
_STEP_TOPICS: dict[str, re.Pattern] = {
    # customer_onboarding
    "security_approval": re.compile(r"\bsecurity\b", re.IGNORECASE),
    "dpa_confirmation": re.compile(
        r"\bdpa\b|\bdata processing (agreement|addendum)\b", re.IGNORECASE
    ),
    "implementation_owner": re.compile(r"\bimplement\w*\b", re.IGNORECASE),
    "customer_handoff": re.compile(r"\bhand[- ]?(off|over)\b", re.IGNORECASE),
    "go_live_date": re.compile(r"\bgo[- ]?live\b|\blaunch\b", re.IGNORECASE),
    # implementation_access
    "access_granted": re.compile(
        r"\baccess\b|\bcredential\w*|\blogin\b|\bpermission\w*|\bapi key\b|\bprovision\w*",
        re.IGNORECASE,
    ),
    "technical_owner": re.compile(
        r"\btechnical (owner|lead|contact|poc)\b|\beng(ineer)?\w* (owner|lead)\b|"
        r"\bdev (owner|lead)\b",
        re.IGNORECASE,
    ),
    "environment_ready": re.compile(
        r"\benvironment\b|\bsandbox\b|\bstaging\b|\btest env\w*|\binstance\b", re.IGNORECASE
    ),
    "integration_scope": re.compile(
        r"\bintegrat\w*|\bwebhook\w*|\bendpoint\w*|\bapi\b", re.IGNORECASE
    ),
    "kickoff_scheduled": re.compile(
        r"\bkick[- ]?off\b|\bstart date\b|\bstart the (build|work|implementation)\b",
        re.IGNORECASE,
    ),
    # decision_quality
    "options_considered": re.compile(
        r"\boptions?\b|\balternativ\w*|\btrade[- ]?offs?\b|\bconsider\w*|\bevaluat\w*",
        re.IGNORECASE,
    ),
    "decision_made": re.compile(
        r"\bdecid\w*|\bdecision\b|\bgo with\b|\bchoose\b|\bchosen\b|\bpick\b", re.IGNORECASE
    ),
    "decision_owner": re.compile(
        r"\b(decision )?owner\b|\bowns\b|\bwho (owns|decides|is deciding|signs off)\b|"
        r"\bfinal say\b",
        re.IGNORECASE,
    ),
    "success_criteria": re.compile(
        r"\bsuccess criteria\b|\bcriteria\b|\bmetric\w*|\bmeasure\w*|"
        r"\bdefinition of done\b|\bkpi\w*",
        re.IGNORECASE,
    ),
    "next_step_defined": re.compile(
        r"\bnext step\w*|\baction item\w*|\bfollow[- ]?up\w*|\bwho[’']?s doing\b|"
        r"\bwhat[’']?s next\b",
        re.IGNORECASE,
    ),
    # meeting_readiness
    "agenda_set": re.compile(r"\bagenda\b", re.IGNORECASE),
    "objective_clear": re.compile(
        r"\bobjective\w*|\bgoal\w*|\bpurpose\b|\boutcome\w*|"
        r"\bwhat.{0,20}(achieve|accomplish|trying to do)\b",
        re.IGNORECASE,
    ),
    "right_attendees": re.compile(
        r"\battend\w*|\bstakeholder\w*|\bright people\b|\binvite\w*|"
        r"\bwho (should|needs to) (be|join|attend)\b",
        re.IGNORECASE,
    ),
    "pre_read_shared": re.compile(
        r"\bpre[- ]?read\w*|\bpre[- ]?work\b|\bmaterials?\b|"
        r"\bshared? (the )?(doc|deck|agenda|brief)\b|\bsent (it |them )?(ahead|in advance|round)\b",
        re.IGNORECASE,
    ),
    "decisions_needed_listed": re.compile(
        r"\bdecisions? (we need|needed|to make|to be made|on the table|required)\b|"
        r"\bneed to decide\b|\bwhat.{0,20}deciding\b",
        re.IGNORECASE,
    ),
}
_step_topic_cache: dict[str, re.Pattern] = {}

# Acronyms that should stay upper-case when a step id is spoken aloud.
_ACRONYMS = {"dpa", "sla", "sso", "mfa", "sow", "poc"}

# Onboarding gets the process-specific phrasing; other templates get a
# generic-but-safe closer.
_INTERVENTION_TAILS = {
    "customer_onboarding": "Should we assign owners before provisioning?",
    "implementation_access": "Should we sort access and an owner before the team starts?",
    "decision_quality": "Should we lock the decision and its owner before we move on?",
    "meeting_readiness": "Should we nail down the objective and the decisions to make first?",
}
_DEFAULT_INTERVENTION_TAIL = "Should we assign owners before we close this out?"

_LIST_CAP = 20  # keep state lists bounded no matter how long the meeting runs
_PERSON_CAP = 8  # per-person list bound (commitments/questions/risks each)


def humanize_step(step: str) -> str:
    """'dpa_confirmation' -> 'DPA confirmation' (spoken-friendly)."""
    words = [w.upper() if w in _ACRONYMS else w for w in step.split("_")]
    return " ".join(words)


def _step_topic(step: str) -> re.Pattern:
    pattern = _STEP_TOPICS.get(step) or _step_topic_cache.get(step)
    if pattern is None:
        words = [re.escape(w) for w in step.split("_") if w]
        pattern = re.compile(r"\b" + r"\W+".join(words) + r"\b", re.IGNORECASE)
        _step_topic_cache[step] = pattern
    return pattern


# "Readiness/discussion" steps are covered simply by being STATED in the meeting
# (an objective named, an agenda set, options weighed) — not by an approval cue.
# A question about them ("what's the agenda?") does not count as covering them.
_DISCUSSION_STEPS = {
    "objective_clear", "agenda_set", "right_attendees", "pre_read_shared",
    "decisions_needed_listed", "options_considered", "success_criteria",
}


def _step_completed(step: str, text: str) -> bool:
    """Called only when the step's topic appears and nothing sounds pending."""
    # Readiness/discussion steps: covered when actually stated, not just asked.
    if step in _DISCUSSION_STEPS:
        return not text.rstrip().endswith("?")
    # Steps that are "done" when a person is put on them.
    if step in ("implementation_owner", "technical_owner", "decision_owner"):
        return bool(_extract_owner(text) or _SELF_OWNER.search(text))
    # Steps that are "done" when a date/schedule is set.
    if step in ("go_live_date", "kickoff_scheduled"):
        return bool(_DATE.search(text) or _SCHEDULED.search(text))
    if step == "customer_handoff":
        return bool(_DONE.search(text) or _SCHEDULED.search(text))
    # A decision counts only when a real decision cue fires (not just "done").
    if step == "decision_made":
        return bool(_DECISION.search(text) or _DONE.search(text))
    # A next step is defined once there's an owner, a date, or a completion cue.
    if step == "next_step_defined":
        return bool(
            _extract_owner(text)
            or _SELF_OWNER.search(text)
            or _DATE.search(text)
            or _DONE.search(text)
        )
    # An environment/access is "done" when it's stood up/ready, not just approved.
    if step == "environment_ready":
        return bool(_READY.search(text) or _DONE.search(text))
    return bool(_DONE.search(text))


def _extract_owner(text: str) -> str:
    for pattern in _OWNER_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return ""


def _person(state: MeetingState, name: str) -> dict | None:
    """The per-person entry for `name`, merging by first name so an extracted
    owner ("Marco") lands on the platform speaker ("Marco Rossi"). Returns
    None when the roster cap is hit (state stays bounded on huge calls)."""
    key = (name or "").strip()
    if not key:
        return None
    first = key.split()[0].lower()
    for existing, entry in state.per_person.items():
        if existing.split()[0].lower() == first:
            return entry
    if len(state.per_person) >= _LIST_CAP:
        return None
    entry = {"lines": 0, "commitments": [], "questions": [], "risks": []}
    state.per_person[key] = entry
    return entry


def _person_append(entries: list, text: str) -> None:
    """Append a per-person snippet with dedupe and a tight cap."""
    norm = text.strip().lower()
    if len(entries) >= _PERSON_CAP:
        return
    if any(str(e).strip().lower() == norm for e in entries):
        return
    entries.append(text)


def _append(entries: list, entry: Any, key: str | None = None) -> None:
    """Append with dedupe (by the entry's text) and a hard cap."""
    if len(entries) >= _LIST_CAP:
        return
    probe = (entry.get(key, "") if key else entry) if isinstance(entry, dict) else entry
    norm = str(probe).strip().lower()
    for existing in entries:
        existing_probe = (
            existing.get(key, "") if key and isinstance(existing, dict) else existing
        )
        if str(existing_probe).strip().lower() == norm:
            return
    entries.append(entry)


# ───────────────────────────── updates ────────────────────────────────
def update(
    state: MeetingState,
    speaker: str,
    text: str,
    *,
    templates: Iterable[ProcessTemplate] = (),
    wake_words: Iterable[str] = (),
) -> MeetingState:
    """Fold one transcript line into the state. Silent, pure regex, O(line)."""
    text = (text or "").strip()
    if not text:
        return state
    if state.stage == "start":
        state.stage = "in_progress"

    # Lock in the meeting type on the first line that matches a template hint.
    if not state.meeting_type:
        for template in templates:
            if _type_hint(template).search(text):
                state.meeting_type = template.id
                state.required_steps = list(template.required_steps)
                state.critical_gaps = list(template.critical_gaps)
                state.missing_steps = list(template.required_steps)
                break

    pending = bool(_PENDING.search(text))

    # Required-step tracking: a step counts as completed only when its topic
    # co-occurs with a completion cue and nothing in the line sounds pending
    # ("we still need security approval" must NOT complete security_approval).
    for step in state.missing_steps[:]:
        if not _step_topic(step).search(text):
            continue
        if pending:
            continue
        if _step_completed(step, text):
            state.completed_steps.append(step)
            state.missing_steps.remove(step)

    if _DECISION.search(text):
        _append(state.decisions, {"speaker": speaker, "decision": text[:200]}, "decision")

    owner = _extract_owner(text)
    if not owner and _SELF_OWNER.search(text) and speaker:
        owner = speaker
    if owner:
        _append(state.owners, {"owner": owner, "item": text[:160]}, "item")

    if _DEADLINE_CUE.search(text):
        when = _DATE.search(text)
        if when:
            _append(
                state.deadlines,
                {"when": when.group(0), "item": text[:160]},
                "item",
            )

    if _RISK.search(text):
        _append(state.risks, {"speaker": speaker, "risk": text[:160]}, "risk")

    lower = text.lower()
    addressed_to_avatar = any(w and w in lower for w in wake_words)
    if (
        text.rstrip().endswith("?")
        and not addressed_to_avatar
        and _PROCESS_QUESTION.search(text)
    ):
        _append(state.open_questions, text[:160])

    # ── per-person tracking ──
    # Fold this line into the speaker's own view (talk share, self-commitments,
    # questions, risks). The avatar's lines are excluded — its name is the wake
    # word. An extracted owner is credited even when someone ELSE assigned it
    # ("Marco will own the rollout" credits Marco, whoever said it).
    wake_set = {str(w).strip().lower() for w in wake_words}
    if speaker and speaker.strip().lower() not in wake_set:
        p = _person(state, speaker)
        if p is not None:
            p["lines"] += 1
            if _SELF_OWNER.search(text):
                _person_append(p["commitments"], text[:120])
            if text.rstrip().endswith("?") and not addressed_to_avatar:
                _person_append(p["questions"], text[:120])
            if _RISK.search(text):
                _person_append(p["risks"], text[:120])
    if owner and owner.strip().lower() not in wake_set:
        target = _person(state, owner)
        if target is not None:
            _person_append(target["commitments"], text[:120])

    if detect_closing(text):
        state.stage = "wrapping_up"
    _evaluate_intervention(state)
    return state


def _evaluate_intervention(state: MeetingState) -> None:
    """Intervene only at the wrap-up, only when a critical step never happened."""
    critical = state.missing_critical()
    if state.stage == "wrapping_up" and state.meeting_type and critical:
        state.should_intervene = True
        state.intervention_reason = "missing critical step(s): " + ", ".join(
            humanize_step(s) for s in critical
        )
    else:
        state.should_intervene = False
        state.intervention_reason = ""


def intervention_line(state: MeetingState) -> str:
    """The one polite sentence Laura says at the end when a critical step is
    missing, e.g. "Before we close, I didn't hear security approval or DPA
    confirmation. Should we assign owners before provisioning?"
    """
    gaps = [humanize_step(s) for s in state.missing_critical()]
    if not gaps:
        return ""
    if len(gaps) == 1:
        heard = gaps[0]
    elif len(gaps) == 2:
        heard = f"{gaps[0]} or {gaps[1]}"
    else:
        heard = ", ".join(gaps[:-1]) + f", or {gaps[-1]}"
    tail = _INTERVENTION_TAILS.get(state.meeting_type, _DEFAULT_INTERVENTION_TAIL)
    return f"Before we close, I didn't hear {heard}. {tail}"


# ───────────────────── session / transcript entry points ──────────────
def observe(session, avatar: Avatar, speaker: str, text: str) -> MeetingState:
    """Fold the newest utterance into the session's live meeting state.

    Call AFTER session.add_utterance(speaker, text): on a fresh state (process
    restart) the existing transcript minus the just-added line is replayed
    first, so the state always reflects the whole meeting.
    """
    state = getattr(session, "meeting_state", None)
    templates = templates_for(avatar)
    if state is None:
        state = MeetingState()
        session.meeting_state = state
        for u in session.transcript[:-1]:
            update(state, u.speaker, u.text, templates=templates, wake_words=avatar.wake_words)
    update(state, speaker, text, templates=templates, wake_words=avatar.wake_words)
    return state


def build_from_text(avatar: Avatar, transcript_text: str) -> MeetingState:
    """Rebuild a full MeetingState from a 'Speaker: line' transcript (used by
    the post-meeting path and the demo, where there is no live session)."""
    state = MeetingState()
    templates = templates_for(avatar)
    for line in (transcript_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        speaker, sep, rest = line.partition(": ")
        if not sep:
            speaker, rest = "", line
        update(state, speaker.strip(), rest, templates=templates, wake_words=avatar.wake_words)
    return state


def state_summary(state: MeetingState) -> str:
    """Compact structured block for prompt injection (no raw transcript)."""
    lines = [
        f"Meeting type: {state.meeting_type or 'unknown'}",
        f"Stage: {state.stage}",
    ]
    if state.required_steps:
        done = ", ".join(humanize_step(s) for s in state.completed_steps) or "none"
        missing = (
            ", ".join(
                humanize_step(s) + (" (CRITICAL)" if s in state.critical_gaps else "")
                for s in state.missing_steps
            )
            or "none"
        )
        lines.append(f"Process steps covered: {done}")
        lines.append(f"Process steps NOT covered: {missing}")
        lines.append(f"Readiness score: {state.readiness_score()}/100")
    if state.decisions:
        lines.append("Decisions: " + "; ".join(d["decision"] for d in state.decisions[:5]))
    if state.owners:
        lines.append(
            "Owners: " + "; ".join(f"{o['owner']} — {o['item']}" for o in state.owners[:5])
        )
    if state.deadlines:
        lines.append(
            "Deadlines: " + "; ".join(f"{d['when']} — {d['item']}" for d in state.deadlines[:5])
        )
    if state.risks:
        lines.append("Risks: " + "; ".join(r["risk"] for r in state.risks[:5]))
    if state.open_questions:
        lines.append("Open questions: " + "; ".join(state.open_questions[:5]))
    # Per-person block: only people with actual content (a bare line count is
    # noise), capped tight — this goes into the latency-critical live prompt.
    person_bits = []
    for name, p in state.per_person.items():
        frags = []
        if p["commitments"]:
            frags.append("committed to: " + " / ".join(p["commitments"][:2]))
        if p["questions"]:
            frags.append("asked: " + p["questions"][-1])
        if p["risks"]:
            frags.append("flagged: " + p["risks"][-1])
        if frags:
            person_bits.append(f"{name} ({p['lines']} turns) — " + "; ".join(frags))
    if person_bits:
        lines.append("Per person:\n  " + "\n  ".join(person_bits[:6]))
    return "\n".join(lines)
