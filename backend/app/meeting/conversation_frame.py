"""PII-safe social-turn state for live meetings.

``MeetingState`` records what the room decided. ``ConversationFrame`` records
what is happening socially right now: who owns the floor, who a turn addresses,
whether an action boundary was crossed, and which avatar would be eligible to
respond.  The live integration is deliberately shadow-only: callers observe the
snapshot but never use it to change speak/silence behavior yet.

Utterance text is accepted only long enough to classify the event.  It is never
stored on the frame or included in ``snapshot()``.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


_QUESTION = re.compile(
    r"^(?:who|what|when|where|why|how|which|can|could|would|will|"
    r"do|does|did|is|are|should|chi|cosa|quando|dove|perch[eé]|come|"
    r"quale|puoi|potresti|possiamo|dobbiamo|[eè])\b",
    re.IGNORECASE,
)
_TOPICS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("calendar", re.compile(r"\b(slot|calendar|meeting|schedule|free time|calendario|riunione)\b", re.I)),
    ("project", re.compile(r"\b(project|task|milestone|asana|jira|progetto|attivit[aà])\b", re.I)),
    ("meeting", re.compile(r"\b(approval|decision|risk|missing|meeting|approvazione|decisione|rischio)\b", re.I)),
)
_STOP = re.compile(r"^\s*(?:wait|stop|pause|hold on|aspetta|fermati|basta)\b", re.I)
_CORRECTION = re.compile(r"^\s*(?:no\b|actually\b|correction\b|anzi\b|in realt[aà]\b)", re.I)
_CONFIRMATION = re.compile(
    r"^\s*(?:yes|yeah|yep|correct|exactly|right|s[iì]|esatto|corretto|giusto)[.!\s]*$",
    re.I,
)


def _meeting_size(count: int) -> str:
    if count <= 0:
        return "empty"
    if count == 1:
        return "one_to_one"
    if count <= 4:
        return "small_group"
    return "large_group"


def _confidence_band(value: Any) -> str:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if confidence < 0.60:
        return "low"
    if confidence < 0.85:
        return "medium"
    return "high"


def _looks_like_question(text: str) -> bool:
    normalized = (text or "").strip()
    return bool(normalized) and (
        normalized.endswith("?") or bool(_QUESTION.search(normalized))
    )


def _infer_topic(text: str) -> str:
    for topic, pattern in _TOPICS:
        if pattern.search(text or ""):
            return topic
    return ""


def classify_interruption(text: str) -> str:
    """Classify an utterance that arrived while an avatar was speaking."""
    normalized = (text or "").strip()
    if _STOP.search(normalized):
        return "stop"
    if _CORRECTION.search(normalized):
        return "correction"
    if _CONFIRMATION.search(normalized):
        return "confirmation"
    return "takeover"


@dataclass
class ConversationFrame:
    """In-memory social state; no transcript text and no persistence."""

    avatar_names: tuple[str, ...]
    capabilities: dict[str, tuple[str, ...]]
    participants: set[str] = field(default_factory=set)
    floor_owner_id: str = ""
    addressed_to: str = "none"
    open_question: bool = False
    selected_avatar: str = "none"
    response_mode: str = "silent"
    duplicate_responses: int = 0
    action_boundary: str = ""
    actions: list[dict[str, str]] = field(default_factory=list)
    handoff_status: str = ""
    interruption_type: str = ""
    response_state: str = "idle"
    speech_generation: int = 0
    speaker_confidence_band: str = "unknown"
    attribution_style: str = "named"
    active_speaking_avatar: str = ""
    active_action_target: str = ""

    @classmethod
    def create(
        cls,
        *,
        avatar_names: Iterable[str],
        capabilities: Mapping[str, Iterable[str]],
    ) -> "ConversationFrame":
        names = tuple(str(name) for name in avatar_names if str(name))
        normalized = {
            name: tuple(str(capability) for capability in capabilities.get(name, ()))
            for name in names
        }
        return cls(avatar_names=names, capabilities=normalized)

    def _select_avatar(self, topic: str) -> str:
        if topic:
            for name in self.avatar_names:
                if topic in self.capabilities.get(name, ()):
                    return name
        return self.avatar_names[0] if self.avatar_names else "none"

    def _observe_utterance(self, event: Mapping[str, Any]) -> None:
        participant_id = str(event.get("participant_id") or "")
        if event.get("speaker_kind", "human") == "human" and participant_id:
            self.floor_owner_id = participant_id

        confidence_band = _confidence_band(event.get("speaker_confidence"))
        if confidence_band != "unknown":
            self.speaker_confidence_band = confidence_band
            self.attribution_style = "neutral" if confidence_band == "low" else "named"

        text = str(event.get("text") or "")
        control_intent = str(event.get("control_intent") or "")
        if control_intent and self.active_speaking_avatar:
            self.interruption_type = control_intent
            if control_intent == "confirmation":
                self.selected_avatar = self.active_speaking_avatar
                self.response_mode = "resume"
                self.response_state = "resume"
            elif control_intent == "correction":
                self.speech_generation += 1
                self.selected_avatar = self.active_speaking_avatar
                self.response_mode = "direct"
                self.response_state = "revise"
            elif control_intent == "stop":
                self.speech_generation += 1
                self.selected_avatar = "none"
                self.response_mode = "silent"
                self.response_state = "cancelled"
                self.active_speaking_avatar = ""
            else:
                self.speech_generation += 1
                self.selected_avatar = "none"
                self.response_mode = "silent"
                self.response_state = "paused"
                self.active_speaking_avatar = ""
            return

        addressed_avatar = str(event.get("addressed_avatar") or "")
        addressed_participant = str(event.get("addressed_participant_id") or "")
        self.open_question = bool(event.get("open_question", _looks_like_question(text)))
        if addressed_avatar:
            self.addressed_to = "avatar"
        elif addressed_participant:
            self.addressed_to = "participant"
        elif self.open_question:
            self.addressed_to = "room"
        else:
            self.addressed_to = "none"

        if self.active_action_target == "avatar" and self.addressed_to == "participant":
            self.action_boundary = "sealed_on_target_change"
            self.active_action_target = ""

        action = event.get("action")
        if isinstance(action, Mapping):
            requester = participant_id or "unassigned"
            self.actions.append(
                {
                    "requested_by": requester,
                    "owner": str(action.get("owner") or "unassigned"),
                    "approver": str(action.get("approver") or requester),
                    "target_account": str(action.get("target_account") or requester),
                }
            )
            self.active_action_target = "avatar" if addressed_avatar else self.addressed_to

        if self.addressed_to == "participant" or not self.open_question:
            self.selected_avatar = "none"
            self.response_mode = "silent"
            return
        if addressed_avatar:
            self.selected_avatar = (
                addressed_avatar if addressed_avatar in self.avatar_names else "none"
            )
        else:
            topic = str(event.get("topic") or _infer_topic(text))
            self.selected_avatar = self._select_avatar(topic)
        self.response_mode = (
            "hand_raise"
            if self.addressed_to == "room" and len(self.participants) >= 5
            else "direct"
        )

    def observe(self, event: Mapping[str, Any]) -> None:
        """Fold one synthetic or live event into the frame."""
        kind = str(event.get("kind") or "")
        participant_id = str(event.get("participant_id") or "")
        if kind == "participant_join":
            if event.get("speaker_kind", "human") == "human" and participant_id:
                self.participants.add(participant_id)
            return
        if kind == "participant_leave":
            self.participants.discard(participant_id)
            if self.floor_owner_id == participant_id:
                self.floor_owner_id = ""
            return
        if kind == "avatar_speech_started":
            avatar = str(event.get("avatar") or "")
            self.active_speaking_avatar = avatar
            self.selected_avatar = avatar if avatar in self.avatar_names else "none"
            self.response_state = "speaking"
            self.speech_generation = int(event.get("generation") or 0)
            return
        if kind == "avatar_handoff":
            target = str(event.get("to_avatar") or "")
            self.addressed_to = "avatar"
            self.selected_avatar = target if target in self.avatar_names else "none"
            self.response_mode = "direct" if self.selected_avatar != "none" else "silent"
            self.handoff_status = "accepted" if self.selected_avatar != "none" else "rejected"
            self.duplicate_responses = 0
            return
        if kind == "utterance":
            self._observe_utterance(event)

    def snapshot(self) -> dict[str, Any]:
        """Return only the stable PII-safe decision surface."""
        result: dict[str, Any] = {
            "meeting_size": _meeting_size(len(self.participants)),
            "participant_count": len(self.participants),
            "addressed_to": self.addressed_to,
            "open_question": self.open_question,
            "selected_avatar": self.selected_avatar,
            "response_mode": self.response_mode,
            "duplicate_responses": self.duplicate_responses,
            "action_count": len(self.actions),
            "speech_generation": self.speech_generation,
            "speaker_confidence_band": self.speaker_confidence_band,
            "attribution_style": self.attribution_style,
            "response_state": self.response_state,
        }
        if self.action_boundary:
            result["action_boundary"] = self.action_boundary
        if self.actions:
            latest = self.actions[-1]
            result.update(
                action_requested_by=latest["requested_by"],
                action_owner=latest["owner"],
                action_approver=latest["approver"],
                action_target_account=latest["target_account"],
            )
        if self.handoff_status:
            result["handoff_status"] = self.handoff_status
        if self.interruption_type:
            result["interruption_type"] = self.interruption_type
        return result


def replay_shadow(
    *,
    events: list[dict[str, Any]],
    avatar_names: list[str],
    capabilities: dict[str, list[str]],
) -> dict[str, Any]:
    """Replay the executable contract without touching live behavior."""
    frame = ConversationFrame.create(
        avatar_names=avatar_names,
        capabilities=capabilities,
    )
    for event in events:
        frame.observe(event)
    return frame.snapshot()


def _session_frame(session: Any, avatar_name: str) -> ConversationFrame:
    frame = getattr(session, "conversation_frame", None)
    if frame is None:
        frame = ConversationFrame.create(
            avatar_names=[avatar_name],
            capabilities={avatar_name: ["meeting"]},
        )
        for participant_id, participant in getattr(session, "participants", {}).items():
            if participant.get("here", True) and participant.get("kind", "human") == "human":
                frame.observe(
                    {
                        "kind": "participant_join",
                        "participant_id": str(participant_id),
                        "speaker_kind": "human",
                    }
                )
        session.conversation_frame = frame
    return frame


def observe_session_participant(
    session: Any,
    *,
    avatar_name: str,
    participant_id: str,
    speaker_kind: str,
    here: bool,
) -> dict[str, Any]:
    frame = _session_frame(session, avatar_name)
    frame.observe(
        {
            "kind": "participant_join" if here else "participant_leave",
            "participant_id": participant_id,
            "speaker_kind": speaker_kind,
        }
    )
    return frame.snapshot()


def observe_session_avatar_speech_started(
    session: Any,
    *,
    avatar_name: str,
    generation: int,
) -> dict[str, Any]:
    frame = _session_frame(session, avatar_name)
    frame.observe(
        {
            "kind": "avatar_speech_started",
            "avatar": avatar_name,
            "generation": generation,
        }
    )
    return frame.snapshot()


def observe_session_utterance(
    session: Any,
    *,
    avatar_name: str,
    participant_id: str,
    speaker_kind: str,
    text: str,
    addressed_avatar: str = "",
    addressed_participant_id: str = "",
    action: Mapping[str, Any] | None = None,
    speaker_confidence: float | None = None,
    topic: str = "",
) -> dict[str, Any]:
    frame = _session_frame(session, avatar_name)
    event: dict[str, Any] = {
        "kind": "utterance",
        "participant_id": participant_id,
        "speaker_kind": speaker_kind,
        "text": text,
        "addressed_avatar": addressed_avatar,
        "addressed_participant_id": addressed_participant_id,
        "action": action,
        "speaker_confidence": speaker_confidence,
        "topic": topic,
    }
    if (
        frame.active_speaking_avatar
        and float(getattr(session, "speaking_until", 0.0) or 0.0) > time.time()
    ):
        event["control_intent"] = classify_interruption(text)
    elif frame.response_state == "speaking":
        frame.active_speaking_avatar = ""
        frame.response_state = "idle"
    frame.observe(event)
    return frame.snapshot()
