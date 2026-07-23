"""Clarify-before-create: an addressed create-ask that lacks what a well-
filed task needs (owner / project / due) makes the avatar ASK once, hold the
approval, and resolve on the asker's reply — no more tasks born ownerless and
invisible. Plus the executor-side net: an Asana task created with no assignee
defaults to the connected account ("me"), so nothing can be orphaned again.

Key-free like the rest of the suite.
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import asana_client, ledger, store, tools
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


@pytest.fixture
def recall_stubbed(monkeypatch):
    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(
        main_module.recall_client,
        "create_bot",
        lambda meeting_url, avatar_page_url, join_at=None, bot_name="Laura",
        avatar_id="": {"id": "bot_cl"},
    )
    monkeypatch.setattr(main_module.recall_client, "leave_call", lambda bot_id: None)
    monkeypatch.setattr(main_module.recall_client, "delete_bot", lambda bot_id: None)
    monkeypatch.setattr(
        main_module.anam_client, "end_conversation", lambda conv_id: None
    )


@pytest.fixture
def spoken(monkeypatch):
    lines: list[str] = []

    async def fake_speak(session, line, **kwargs):
        lines.append(line)
        return True

    monkeypatch.setattr(main_module, "_make_avatar_speak", fake_speak)
    return lines


@pytest.fixture
def approved(monkeypatch):
    # These tests exercise the voice-consent flavour of the clarify loop;
    # the code default is approval-first (voice_consent_writes False).
    monkeypatch.setattr(settings, "voice_consent_writes", True)
    calls: list[dict] = []
    monkeypatch.setattr(
        main_module.cedric, "voice_approve",
        lambda session, item: calls.append(dict(item)) or True,
    )
    # voice_approve is fired fire-and-forget:
    #   asyncio.create_task(run_in_threadpool(cedric.voice_approve, ...)).
    # The created task can outlive the request, so asserting `approved` right
    # after the webhook returns races it (order-/timing-dependent, flaky under
    # load). Run run_in_threadpool's target EAGERLY at call time — the capture
    # then happens synchronously (before create_task defers) while awaited
    # results are preserved. Scoped to these clarify tests only.
    def _eager_threadpool(fn, *a, **k):
        result = fn(*a, **k)

        async def _done():
            return result

        return _done()

    monkeypatch.setattr(main_module, "run_in_threadpool", _eager_threadpool)
    return calls


START_BODY = {
    "meeting_url": "https://meet.google.com/cl-test-one",
    "avatar_id": "cedric",
    "callback_url": "https://cedric.example/api/meet/callback",
}


def _say(client, bot_id: str, text: str) -> dict:
    return client.post(
        "/webhooks/recall",
        json={
            "event": "transcript.data",
            "data": {
                "bot": {"id": bot_id},
                "data": {
                    "words": [{"text": w} for w in text.split()],
                    "participant": {"name": "Ben", "id": 1},
                },
            },
        },
    ).json()


def _age_clarify(session, seconds: float = 10.0) -> None:
    """Move the clarify timestamp back so the next line reads as an ANSWER
    (in real meetings the avatar's spoken question already consumed >4s)."""
    it, spk, ts, missing, ek, fp = session.pending_clarify
    session.pending_clarify = (it, spk, ts - seconds, missing, ek, fp)


# ───────────────────────── the clarify loop ─────────────────────────
def test_full_details_skip_clarify(client, recall_stubbed, spoken, approved):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(
        client, bot_id,
        "Cedric, create a task to send the recap, assigned to Dana, "
        "in the launch project, due Friday, the description should say "
        "prep the notes",
    )
    assert body.get("action_capture") is True
    assert "clarifying" not in body
    assert len(approved) == 1  # nothing missing → approved immediately
    assert spoken[-1] in main_module._VOICE_LINES + main_module._VOICE_LINES_IT


def test_missing_details_ask_then_answer(client, recall_stubbed, spoken, approved):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(client, bot_id, "Cedric, please create a task in Asana")
    assert body.get("clarifying") == ["task_name"]
    assert approved == []  # held — nothing approved yet
    assert spoken[-1].startswith("Got it — I'll queue that for your approval.")
    assert "call the task" in spoken[-1]

    session = store.get(bot_id)
    _age_clarify(session)
    body = _say(client, bot_id, "Call the task help ducho")
    assert body.get("clarified") is True
    assert len(approved) == 1
    action_text = session.queued_actions[0]["action"].lower()
    assert "task name: help ducho" in action_text
    assert spoken[-1] in main_module._VOICE_LINES + main_module._VOICE_LINES_IT


def test_skip_answer_cannot_waive_required_task_name(
    client, recall_stubbed, spoken, approved
):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    _say(client, bot_id, "Cedric, create a task in Asana")
    session = store.get(bot_id)
    _age_clarify(session)
    original = session.queued_actions[0]["action"]

    body = _say(client, bot_id, "no one, just create it")
    assert body.get("clarifying") == ["task_name"]
    assert approved == []
    assert session.queued_actions[0]["action"] == original  # skip words not glued on


def test_new_ask_resolves_stale_pending(client, recall_stubbed, spoken, approved):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    _say(client, bot_id, "Cedric, create a task in Asana")
    session = store.get(bot_id)
    # The asker never answers — they fire a NEW complete ask instead.
    _age_clarify(session, seconds=50.0)  # also past the answer window
    body = _say(
        client, bot_id,
        "Cedric, create a task to email Marco, assigned to Dana, "
        "in the launch project, by Friday, the description should say "
        "send the weekly update",
    )
    assert body.get("action_capture") is True
    # The stale incomplete card remains needs-details; only the complete new
    # task reaches approval.
    assert len(approved) == 1
    assert "email marco" in approved[0]["action"].lower()
    assert len(session.queued_actions) == 2


def test_default_resolution_queues_for_approval(
    client, recall_stubbed, spoken, monkeypatch
):
    """Code default (approval-first, 2026-07-18): the clarify answer resolves
    the capture into the approval QUEUE — no voice approval fires, and the
    spoken ack points at the dashboard, not at Cedric running it."""
    monkeypatch.setattr(
        main_module.cedric, "voice_approve",
        lambda *a: pytest.fail("default must not voice-approve"),
    )
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    _say(client, bot_id, "Cedric, please create a task in Asana")
    session = store.get(bot_id)
    _age_clarify(session)
    body = _say(client, bot_id, "Call the task help ducho")
    assert body.get("clarified") is True
    assert spoken[-1] in (main_module._QUEUE_LINES + main_module._QUEUE_LINES_IT
                          + main_module._QUEUE_LINES_TASK + main_module._QUEUE_LINES_TASK_IT)
    assert len(session.queued_actions) == 1  # captured, waiting for the click


def test_flag_off_keeps_immediate_confirmation(
    client, recall_stubbed, spoken, approved, monkeypatch
):
    monkeypatch.setattr(settings, "clarify_before_create", False)
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(client, bot_id, "Cedric, create a task called bare minimum")
    assert "clarifying" not in body
    assert len(approved) == 1


# ───────────────────────── the detail heuristics ─────────────────────────
def test_missing_action_details_cases():
    m = tools.missing_action_details
    assert m("create a task called help ducho") == []
    assert m("create a task, assigned to Dana, in the launch project, due Friday, "
             "the description should say prep the notes") == []
    assert m("create a task in Asana") == ["task_name"]
    assert m("create a task called basic task") == []
    assert m("") == ["task_name"]


def test_is_detail_skip_cases():
    assert tools.is_detail_skip("no one, just create it")
    assert tools.is_detail_skip("doesn't matter, skip it")
    assert not tools.is_detail_skip("Dana should own it")


# ──────────────── executor net: no task is born invisible ────────────────
def test_create_task_defaults_assignee_to_me(monkeypatch):
    posted: list[dict] = []
    monkeypatch.setattr(asana_client, "_token", lambda org: ("pat_x", ""))
    monkeypatch.setattr(asana_client, "_workspace_gid", lambda pat: ("ws1", ""))
    monkeypatch.setattr(asana_client, "_resolve_project", lambda org, p: ("", ""))
    monkeypatch.setattr(
        asana_client, "_post",
        lambda pat, path, body, **kw: posted.append(body)
        or {"ok": True, "task": {"gid": "t1"}},
    )
    asana_client.create_task("org_x", {"name": "Help Ducho"})
    assert posted[0]["assignee"] == "me"  # never ownerless-and-invisible again

    asana_client.create_task("org_x", {"name": "Other", "assignee": "dana@x.com"})
    assert posted[1]["assignee"] == "dana@x.com"  # explicit assignee always wins


# ───────────── solo-fluid capture (owner ask 2026-07-21) ─────────────
def test_solo_instruction_captures_without_wake_word(client, recall_stubbed, spoken):
    """One human in the roster: a direct instruction with NO name is still an
    unambiguous ask for the avatar — captured + confirmed, not left to the
    summarizer (round-3 live repro: solo asks became mere 'goals')."""
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(
        client, bot_id,
        "Please create a task to send the recap, assigned to Dana, "
        "in the launch project, due Friday",
    )
    assert body.get("action_capture") is True
    assert spoken and spoken[-1]  # a confirmation line was spoken


def test_group_instruction_still_requires_the_name(client, recall_stubbed, spoken):
    """Two humans present: an unaddressed 'someone should…' stays the
    summarizer's job — capture keeps requiring the name in groups."""
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    _say(client, bot_id, "Hi everyone, thanks for joining")  # speaker: Ben
    client.post(
        "/webhooks/recall",
        json={"event": "transcript.data",
              "data": {"bot": {"id": bot_id},
                       "data": {"words": [{"text": "hello"}],
                                "participant": {"name": "Alice", "id": 2}}}},
    )
    body = _say(
        client, bot_id,
        "Please create a task to send the recap, assigned to Dana, "
        "in the launch project, due Friday",
    )
    assert body.get("action_capture") is not True


def test_back_to_back_instructions_capture_separately(client, recall_stubbed, spoken):
    """Round-4 live repro: a second complete ask seconds after the first was
    glued onto it as a 'continuation' — one monster item, no confirmation for
    ask #2. A follow-up that itself reads as a new ask must start a NEW capture
    (and, details missing, the clarify loop)."""
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    r1 = _say(
        client, bot_id,
        "Create a task called research kickoff, assigned to Dana, "
        "in the research project, due Friday, the description should say "
        "kick off the research",
    )
    assert r1.get("action_capture") is True
    r2 = _say(client, bot_id,
              "Also create a task to email Duccio the summary, assigned to me, "
              "in the research project, due Friday, the description should say "
              "send the summary")
    assert r2.get("action_capture") is True or r2.get("clarifying")
    caps = store.get(bot_id).queued_actions
    texts = [str(c.get("action")) for c in caps]
    assert len(caps) == 2, texts
    assert "Also create a task" in texts[1] and "Also create" not in texts[0]


# ───────────────── kind-aware clarify (2026-07-21 live repro) ─────────────────
# "send an email to Duccio" used to get the Asana owner/project/due
# questionnaire; a Slack message got interrogated too. The slots now follow
# the ask's kind, and free-form asks never interrogate.


def test_email_ask_gets_email_questions(client, recall_stubbed, spoken, approved):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(client, bot_id, "Cedric, can you send an email to duccio@example.com")
    assert body.get("clarifying") == ["email_body"]
    assert "what it should say" in spoken[-1]
    assert "who should own it" not in spoken[-1]
    assert approved == []


def test_email_ask_missing_everything_asks_both(
    client, recall_stubbed, spoken, approved
):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(client, bot_id, "Cedric, can you send an email")
    # Combined single ask (restored 2026-07-22 feel), not slot-by-slot.
    assert body.get("clarifying") == ["email_to", "email_body"]
    assert "who it should go to" in spoken[-1]
    assert "what it should say" in spoken[-1]


def test_calendar_ask_gets_calendar_questions(
    client, recall_stubbed, spoken, approved
):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(
        client, bot_id, "Cedric, can you create a new meeting between me and Duccio"
    )
    assert body.get("clarifying") == ["invite_when"]
    assert "when it should be" in spoken[-1]
    assert "which project" not in spoken[-1]


def test_freeform_ask_never_interrogates(client, recall_stubbed, spoken, approved):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(client, bot_id, "Cedric, please message Dana on Slack saying hi")
    assert body.get("action_capture") is True
    assert "clarifying" not in body
    assert len(approved) == 1  # confirmed and queued straight away


# ───────────── same-intent retry damping (four cards for one email) ─────────────


def test_repeated_ask_merges_instead_of_multiplying(
    client, recall_stubbed, spoken, approved
):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(client, bot_id, "Cedric, can you send an email to duccio@example.com")
    assert body.get("clarifying") == ["email_body"]
    session = store.get(bot_id)

    # Unaddressed retry (louder, ASR-mangled) — a restatement, not an answer.
    _age_clarify(session)
    body = _say(client, bot_id, "could you send an email to duccio@example.com please")
    assert body.get("restated") is True
    assert approved == []
    assert session.pending_clarify is not None

    # Addressed retry — same damping through the capture gate.
    _age_clarify(session)
    body = _say(client, bot_id, "Cedric, please send an email to duccio@example.com")
    assert body.get("restated") is True
    assert approved == []

    # The actual answer resolves the ONE pending item.
    _age_clarify(session)
    body = _say(client, bot_id, "it should say hello from the meeting")
    assert body.get("clarified") is True
    assert len(approved) == 1
    assert len(session.queued_actions) == 1


def test_resolved_capture_retry_merges(client, recall_stubbed, spoken, approved):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(client, bot_id, "Cedric, send an email to dana@example.com saying hi")
    assert body.get("action_capture") is True and "clarifying" not in body
    assert len(approved) == 1
    session = store.get(bot_id)

    body = _say(client, bot_id, "Cedric, can you send an email to dana@example.com")
    assert body.get("merged") is True
    assert len(approved) == 1  # no second approval
    assert len(session.queued_actions) == 1  # still ONE card
    assert spoken[-1] in main_module._ALREADY_LINES + main_module._ALREADY_LINES_IT


def test_different_recipient_is_a_new_action(
    client, recall_stubbed, spoken, approved
):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    _say(client, bot_id, "Cedric, send an email to dana@example.com saying hi")
    body = _say(client, bot_id, "Cedric, send an email to marco@example.com saying ciao")
    assert body.get("merged") is None
    assert len(approved) == 2  # genuinely two emails
    session = store.get(bot_id)
    assert len(session.queued_actions) == 2


def test_email_clarify_binds_one_action_through_a_live_session(
    client, recall_stubbed, spoken, approved
):
    """End-to-end through the webhook: the four-utterance email clarify
    ("send an email" → recipient name → spoken domain → body → "yes") binds to
    ONE action the whole way. The proposed address stays a non-executable
    candidate, the body attaches to the same card, and nothing is approved
    until the recipient is confirmed."""
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    session = store.get(bot_id)

    # 1) Bare ask → held, asking who + what (single combined ask).
    body = _say(client, bot_id, "Cedric, can you send an email?")
    assert body.get("clarifying") == ["email_to", "email_body"]
    assert approved == []
    assert len(session.queued_actions) == 1
    aid = session.queued_actions[0]["action_id"]

    # 2) Recipient NAME only — a name is not an address, so still held.
    _age_clarify(session)
    body = _say(client, bot_id, "Send it to Anant.")
    assert body.get("clarifying") == ["email_to", "email_body"]
    assert approved == []

    # 3) Spoken domain → a Recipient CANDIDATE: the slot advances from a raw
    #    address to email_to_confirm, and the card is STILL held (unapproved).
    _age_clarify(session)
    body = _say(client, bot_id, "At s f f studio dot com.")
    assert body.get("clarifying") == ["email_to_confirm", "email_body"]
    assert approved == []

    # 4) Body attaches to the SAME card; recipient still needs confirmation.
    _age_clarify(session)
    body = _say(client, bot_id, "The body should say the demo is ready.")
    assert body.get("clarifying") == ["email_to_confirm"]  # body bound, confirm left
    assert approved == []  # approval BLOCKED until the address is confirmed

    # 5) "Yes" confirms the candidate → exactly one approved action, with the
    #    address promoted to a real Recipient and the body bound to it.
    _age_clarify(session)
    body = _say(client, bot_id, "Yes")
    assert body.get("clarified") is True
    assert len(approved) == 1
    assert len(session.queued_actions) == 1  # ONE card the whole way
    final = approved[-1]
    assert final["action_id"] == aid  # ONE action ID throughout
    params = tools.collected_action_parameters(final["action"])
    assert params.get("recipient") == "anant@sffstudio.com"
    assert params.get("body") == "the demo is ready"
    # Confirmed, not a leftover candidate, and never a tracked-only dead card.
    assert "recipient candidate" not in final["action"].lower()
    assert "tracked only" not in final["action"].lower()


def test_ask_kind_and_same_ask_units():
    assert tools.ask_kind("send an email to Duccio") == "email"
    assert tools.ask_kind("create a new meeting between me and Ducho") == "calendar"
    assert tools.ask_kind("create an Asana task called launch") == "task"
    assert tools.ask_kind("message Ben on Slack") == "other"
    # task wins ties: filing a task ABOUT an email is a task
    assert tools.ask_kind("create a task to email the recap") == "task"
    a = "Can you send an email to Duccio please. Still talking."
    assert tools.same_ask(a, "Patrick, can you send an email to please?")
    assert tools.same_ask(a, "So, , could you send an email to do choke")
    assert not tools.same_ask(a, "Can you create a new meeting between me and Ducho?")
    assert not tools.same_ask(a, "send an email to Ben saying hi")
