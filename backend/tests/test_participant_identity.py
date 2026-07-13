"""Canonical multiparty identity and agent-speech isolation regressions."""
from __future__ import annotations

import asyncio
import json

from app import avatars, main, meeting_state, store
from app.config import settings


def _session(tmp_path, monkeypatch, bot_id: str) -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "identity.sqlite3")
    monkeypatch.setattr(settings, "recall_api_key", "")
    store._init_db()
    session = store.create(
        bot_id,
        "https://meet.google.com/abc-defg-hij",
        "laura",
    )
    session.memory_brief = ""
    return session


def _post(payload: dict) -> dict:
    class FakeRequest:
        headers: dict = {}
        query_params: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    response = asyncio.run(main.recall_webhook(FakeRequest()))
    return json.loads(response.body)


def _participant_payload(
    bot_id: str,
    event: str,
    *,
    participant_id: str,
    name: str | None,
    text: str = "",
    **metadata,
) -> dict:
    participant = {"id": participant_id, "name": name, **metadata}
    data = {"participant": participant}
    if text:
        data["words"] = [{"text": word} for word in text.split()]
    return {
        "event": event,
        "data": {"bot": {"id": bot_id}, "data": data},
    }


def test_missing_final_name_resolves_from_participant_id(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, "identity-name")
    _post(
        _participant_payload(
            session.bot_id,
            "participant_events.join",
            participant_id="2",
            name="Marco",
        )
    )
    _post(
        _participant_payload(
            session.bot_id,
            "transcript.data",
            participant_id="2",
            name=None,
            text="Kickoff for the customer onboarding",
        )
    )

    assert session.transcript[-1].speaker == "Marco"
    assert session.transcript[-1].participant_id == "2"
    store.remove(session.bot_id)


def test_duplicate_alex_participants_remain_distinct():
    state = meeting_state.MeetingState()
    meeting_state.update(
        state,
        "Alex",
        "I'll take the security follow-up.",
        participant_id="alex-1",
    )
    meeting_state.update(
        state,
        "Alex",
        "I'll take the DPA follow-up.",
        participant_id="alex-2",
    )

    assert set(state.per_person) == {"alex-1", "alex-2"}
    assert state.per_person["alex-1"]["commitments"] == [
        "I'll take the security follow-up."
    ]
    assert state.per_person["alex-2"]["commitments"] == [
        "I'll take the DPA follow-up."
    ]
    assert {owner["owner_id"] for owner in state.owners} == {"alex-1", "alex-2"}


def test_human_sharing_avatar_display_name_is_not_agent(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, "identity-human-laura")
    identity = session.resolve_participant("Laura", "human-7", metadata={})

    assert identity["kind"] == "human"
    assert not main._is_own_speech("Laura", identity["name"], identity["kind"])

    body = _post(
        _participant_payload(
            session.bot_id,
            "transcript.data",
            participant_id="human-7",
            name="Laura",
            text="Kickoff onboarding and security is approved",
        )
    )
    assert body.get("reason") != "own speech"
    assert session.transcript[-1].speaker_kind == "human"
    assert "security_approval" in session.meeting_state.completed_steps
    store.remove(session.bot_id)


def test_agent_assertion_stays_in_transcript_but_not_meeting_state(
    tmp_path, monkeypatch
):
    session = _session(tmp_path, monkeypatch, "identity-agent")
    avatar = avatars.load("laura")
    session.add_utterance(
        "Marco",
        "Kickoff for the customer onboarding",
        participant_id="human-2",
    )
    state = meeting_state.observe(
        session,
        avatar,
        "Marco",
        "Kickoff for the customer onboarding",
        participant_id="human-2",
    )
    assert "security_approval" in state.missing_steps

    body = _post(
        _participant_payload(
            session.bot_id,
            "transcript.data",
            participant_id="bot-participant",
            name="Laura",
            text="Security is approved and we decided to ship",
            is_bot=True,
        )
    )

    assert body.get("reason") == "own speech"
    assert session.transcript[-1].speaker_kind == "agent"
    assert "security_approval" in state.missing_steps
    assert not state.decisions
    assert "Security is approved" not in session.transcript_text(
        include_agents=False
    )
    store.remove(session.bot_id)


def test_restart_preserves_identity_kind_and_rebuild_classification(
    tmp_path, monkeypatch
):
    session = _session(tmp_path, monkeypatch, "identity-restart")
    avatar = avatars.load("laura")
    marco = session.participant_event(
        "Marco", "2", here=True, metadata={"id": "2"}
    )
    agent = session.participant_event(
        "Laura",
        "bot-participant",
        here=True,
        metadata={"id": "bot-participant", "is_bot": True},
    )
    session.add_utterance(
        marco["name"],
        "Kickoff for the customer onboarding",
        participant_id=marco["id"],
        speaker_kind=marco["kind"],
    )
    session.add_utterance(
        agent["name"],
        "Security is approved",
        participant_id=agent["id"],
        speaker_kind=agent["kind"],
    )

    store._sessions.clear()
    store._load_from_db()
    restored = store.get("identity-restart")
    assert restored is not None
    assert restored.participants["2"]["name"] == "Marco"
    assert restored.participants["bot-participant"]["kind"] == "agent"
    assert [u.participant_id for u in restored.transcript] == [
        "2",
        "bot-participant",
    ]

    # observe() replays the preceding persisted utterances with their kinds.
    state = meeting_state.observe(
        restored,
        avatar,
        restored.transcript[-1].speaker,
        restored.transcript[-1].text,
        participant_id=restored.transcript[-1].participant_id,
        speaker_kind=restored.transcript[-1].speaker_kind,
    )
    assert "security_approval" in state.missing_steps
    assert not state.decisions
    store.remove(restored.bot_id)
