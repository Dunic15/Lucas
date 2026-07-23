"""Canonical pending-action binding for live meeting clarifications.

The state contains distilled action parameters only. It is keyed by meeting,
speaker and action id, lives for the meeting session, and never authorizes an
external write. In particular, synthesized email addresses remain candidates
until the speaker explicitly confirms them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
from typing import Any, Iterator

from . import action_plane


_KIND_TYPE = {
    "email": "email.send",
    "calendar": "calendar.create_event",
    "task": "asana.create_task",
}
_YES = re.compile(r"^\s*(?:yes|yeah|yep|correct|that's right|that is right|s[iì])\s*[.!]*$", re.I)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_BODY = re.compile(
    r"^(?:the\s+)?body\s+(?:should\s+say|is|says)\s+|"
    r"^(?:it\s+)?should\s+say\s+|^(?:saying|that\s+says)\s+",
    re.I,
)
_TASK_NAME = re.compile(
    r"^(?:call|name)\s+(?:the\s+)?task\s+|"
    r"^(?:the\s+)?task\s+(?:name\s+)?(?:should\s+be|is)\s+|"
    r"^task\s+name:\s*",
    re.I,
)
_RECIPIENT = re.compile(
    r"^(?:and\s+)?(?:send\s+(?:it|the\s+email)\s+)?to\s+"
    r"(?P<name>[A-ZÀ-Ù][\wÀ-ù.'-]{1,60})[.!]*$",
    re.I,
)
_QUESTION = re.compile(
    r"^\s*(?:do|did|have|has|can|could|would|will|what|where|when|why|how|who|is|are)\b",
    re.I,
)
_ORPHAN_FRAGMENT = re.compile(
    r"^\s*(?:and\s+send\s+(?:it\s+)?to\b|at\s+(?:[a-z]\s+){2,}"
    r"|due\s+\w+\b|in\s+the\s+.+\s+(?:project|board|backlog)\b)",
    re.I,
)


def _expected(missing: list[str]) -> str:
    order = (
        "email_to", "email_to_domain", "email_to_confirmation", "email_body",
        "task_name", "invite_with", "invite_when",
    )
    return next((name for name in order if name in missing), missing[0] if missing else "")


def _question(state: "PendingAction") -> str:
    missing = state.required_missing_parameters
    collected = state.collected_parameters
    if "email_to_domain" in missing:
        who = str(collected.get("recipient_name") or "the recipient")
        tail = " I also still need what it should say." if "email_body" in missing else ""
        return f"What is {who}'s exact email address or domain?{tail}"
    if "email_to_confirmation" in missing:
        candidate = str(collected.get("recipient_candidate") or "")
        tail = " I also still need what it should say." if "email_body" in missing else ""
        return f"Should I use {candidate}?{tail}"
    if missing == ["email_body"]:
        return "What should the email say?"
    if missing == ["task_name"]:
        return "What should the task be called?"
    return state.last_clarification_question


@dataclass
class PendingAction:
    meeting_id: str
    speaker_id: str
    item: dict
    created_at: float
    required_missing_parameters: list[str]
    source_event_key: str = ""
    source_fingerprint: str = ""
    last_clarification_question: str = ""
    collected_parameters: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)
    status: str = "needs_details"

    @property
    def action_id(self) -> str:
        return str(self.item.get("action_id") or "")

    @property
    def action_type(self) -> str:
        return _KIND_TYPE.get(str(self.collected_parameters.get("kind") or ""), "")

    @property
    def expected_answer_field(self) -> str:
        return _expected(self.required_missing_parameters)

    @property
    def key(self) -> str:
        return f"{self.meeting_id}:{self.speaker_id}:{self.action_id}"

    def __iter__(self) -> Iterator[Any]:
        # Compatibility with the legacy six-value tuple while callers migrate
        # to named canonical fields.
        yield self.item
        yield self.speaker_id
        yield self.created_at
        yield list(self.required_missing_parameters)
        yield self.source_event_key
        yield self.source_fingerprint

    def __getitem__(self, index: int) -> Any:
        return tuple(iter(self))[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "collected_parameters": dict(self.collected_parameters),
            "required_missing_parameters": list(self.required_missing_parameters),
            "last_clarification_question": self.last_clarification_question,
            "expected_answer_field": self.expected_answer_field,
            "updated_at": self.updated_at,
            "status": self.status,
        }

    def refresh(
        self, item: dict, missing: list[str], question: str = ""
    ) -> "PendingAction":
        self.item = item
        self.required_missing_parameters = list(missing)
        self.updated_at = time.time()
        self.status = "needs_details" if missing else "proposed"
        if question:
            self.last_clarification_question = question
        self.last_clarification_question = _question(self)
        return self


def create(
    meeting_id: str,
    speaker_id: str,
    item: dict,
    missing: list[str],
    *,
    source_event_key: str = "",
    source_fingerprint: str = "",
    question: str = "",
    kind: str = "",
) -> PendingAction:
    state = PendingAction(
        meeting_id=str(meeting_id or ""),
        speaker_id=str(speaker_id or ""),
        item=item,
        created_at=time.time(),
        required_missing_parameters=list(missing),
        source_event_key=str(source_event_key or ""),
        source_fingerprint=str(source_fingerprint or ""),
        last_clarification_question=str(question or ""),
        collected_parameters={
            "kind": str(kind or ""),
            "base_action": str(item.get("action") or "").strip(" ."),
        },
    )
    return state.refresh(item, missing, question)


def is_orphan_fragment(text: str) -> bool:
    """Incomplete executable detail that must never become its own action."""
    return bool(_ORPHAN_FRAGMENT.search(str(text or "")))


def _spoken_domain(text: str) -> str:
    value = str(text or "").strip().lower().strip(" .!?")
    value = re.sub(r"^(?:at\s+)", "", value)
    value = re.sub(r"\s+dot\s+", ".", value)
    if "." not in value:
        return ""
    labels = [
        re.sub(r"[^a-z0-9-]", "", label)
        for label in value.split(".")
    ]
    if len(labels) < 2 or any(not label for label in labels):
        return ""
    domain = ".".join(labels)
    return domain if re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain) else ""


def _render_email(state: PendingAction) -> dict[str, str]:
    c = state.collected_parameters
    parts = [str(c.get("base_action") or "").strip(" .")]
    recipient = str(c.get("recipient_name") or "")
    confirmed = c.get("to")
    if confirmed:
        parts.append("Recipient: " + str(confirmed[0]))
    elif recipient:
        parts.append(f"Recipient: {recipient} (address unconfirmed)")
    if c.get("body"):
        parts.append("Body: " + str(c["body"]))
    return {"action": ". ".join(p for p in parts if p)[:300]}


def _render_task(state: PendingAction) -> dict[str, str]:
    c = state.collected_parameters
    parts = [str(c.get("base_action") or "").strip(" .")]
    if c.get("name"):
        parts.append("Task name: " + str(c["name"]))
    return {"action": ". ".join(p for p in parts if p)[:300]}


def bind(state: PendingAction, text: str) -> tuple[bool, dict[str, str], list[str], str]:
    """Bind one utterance to the expected slot, returning a deterministic edit."""
    raw = " ".join(str(text or "").split()).strip()
    missing = list(state.required_missing_parameters)
    c = state.collected_parameters
    kind = str(c.get("kind") or "")

    # Explicit field answers may contain a question mark in their body. Only
    # unrelated interrogatives bypass binding.
    explicit = bool(_EMAIL.search(raw) or _BODY.search(raw) or _TASK_NAME.search(raw))
    if (raw.endswith("?") or _QUESTION.search(raw)) and not explicit:
        return False, {}, missing, state.last_clarification_question

    if kind == "email":
        address = _EMAIL.search(raw)
        if address:
            email = address.group(0).lower()
            c["to"] = [email]
            c["recipient_name"] = c.get("recipient_name") or email.split("@", 1)[0]
            c["recipient_confirmed"] = True
            missing = [
                x for x in missing
                if x not in ("email_to", "email_to_domain", "email_to_confirmation")
            ]
        elif "email_to_confirmation" in missing and _YES.fullmatch(raw):
            candidate = str(c.get("recipient_candidate") or "")
            if not _EMAIL.fullmatch(candidate):
                return False, {}, missing, state.last_clarification_question
            c["to"] = [candidate]
            c["recipient_confirmed"] = True
            missing.remove("email_to_confirmation")
        else:
            rec = _RECIPIENT.fullmatch(raw)
            if rec and "email_to" in missing:
                c["recipient_name"] = rec.group("name").strip(" .")
                missing = [
                    "email_to_domain" if x == "email_to" else x for x in missing
                ]
            else:
                domain = _spoken_domain(raw)
                if domain and (
                    "email_to" in missing or "email_to_domain" in missing
                ) and c.get("recipient_name"):
                    local = re.sub(
                        r"[^a-z0-9._+-]", "",
                        str(c["recipient_name"]).lower(),
                    )
                    candidate = f"{local}@{domain}"
                    c["recipient_candidate"] = candidate
                    c["recipient_domain"] = domain
                    c["recipient_confirmed"] = False
                    missing = [
                        x for x in missing if x not in ("email_to", "email_to_domain")
                    ]
                    if "email_to_confirmation" not in missing:
                        missing.insert(0, "email_to_confirmation")
                else:
                    body = _BODY.sub("", raw).strip(" .")
                    if body != raw.strip(" .") and "email_body" in missing and body:
                        c["body"] = body
                        missing.remove("email_body")
                    else:
                        return False, {}, missing, state.last_clarification_question
        # A body can be supplied even while address confirmation is pending.
        body = _BODY.sub("", raw).strip(" .")
        if body != raw.strip(" .") and "email_body" in missing and body:
            c["body"] = body
            missing.remove("email_body")
        state.refresh(state.item, missing)
        return True, _render_email(state), missing, state.last_clarification_question

    if kind == "task" and "task_name" in missing:
        candidate = _TASK_NAME.sub("", raw).strip(" .")
        if candidate == raw.strip(" .") and len(raw.split()) > 12:
            return False, {}, missing, state.last_clarification_question
        if not action_plane.is_meaningful_task_name(candidate):
            return True, {}, missing, "What should the task be called?"
        c["name"] = candidate
        missing.remove("task_name")
        state.refresh(state.item, missing)
        return True, _render_task(state), missing, state.last_clarification_question

    return False, {}, missing, state.last_clarification_question
