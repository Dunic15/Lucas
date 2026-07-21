"""Cross-language action dedup: one spoken request must never execute twice.

Production incident (2026-07-10): the room asked live, in Italian, "schedula un
meeting di prova con Ben domani alle 15 per la review del flusso": captured by
queue_action with a live action_id. At finalize the summarizer re-extracted the
SAME request in English ("Schedule a test meeting with Ben tomorrow at 15:00
for the flow review"). The #84 word-overlap dedup can't bridge the language gap
(no shared content words), so the artifact carried TWO actions → two Slack
approval cards → both approved → two calendar events.

Two-layer fix, both covered here:
  (a) prevention at the source; post_meeting shows the live captures to the
      summarizer with an explicit do-not-re-extract (any language) instruction;
  (b) safety net at the merge; brain.semantic_action_duplicates, one cheap
      completion at finalize (stub: no-op; failure: fail open, keep both).

Key-free like the rest of the suite: recall/anam are monkeypatched and every
model call is stubbed at the llm/function seam.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app.meeting import lifecycle  # noqa: E402  (lifecycle hoisted from main)
from app import avatars, ledger, store, tools
from app.brain import engine as brain
from app.config import settings

# The exact production pair.
IT_LIVE = "schedula un meeting di prova con Ben domani alle 15 per la review del flusso"
EN_EXTRACTED = "Schedule a test meeting with Ben tomorrow at 15:00 for the flow review"

MATCH_JSON = '{"duplicates": [{"extracted": 0, "live": 0}]}'


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
        lambda *a, **k: {"id": "bot_1"},
    )
    monkeypatch.setattr(main_module.recall_client, "leave_call", lambda bot_id: None)
    monkeypatch.setattr(main_module.recall_client, "delete_bot", lambda bot_id: None)
    monkeypatch.setattr(
        main_module.anam_client, "end_conversation", lambda conv_id: None
    )


@pytest.fixture
def real_post_provider(monkeypatch):
    """Force the non-stub post path so the cross-language net actually runs;
    the completion itself is stubbed per-test at brain.llm.complete."""
    monkeypatch.setattr(settings, "brain_provider_post", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")


# ── (b) the merge-level net: EXACT production repro ─────────────────────


def test_merge_dedupes_crosslang_summarizer_duplicate(monkeypatch, real_post_provider):
    """IT live capture vs its EN re-extraction: word overlap can't match them
    (no shared content words), the semantic net must; live entry wins, keeps
    its live action_id, absorbs the summarizer's structured deadline/owner."""
    prompts: list[str] = []

    def fake_complete(system, user, **kw):
        prompts.append(user)
        return MATCH_JSON

    monkeypatch.setattr(brain.llm, "complete", fake_complete)

    out = main_module._merge_action_items(
        [{"action_id": "live_it", "action": IT_LIVE, "owner": "", "due": ""}],
        [{"item": EN_EXTRACTED, "owner": "Ben",
          "deadline": "tomorrow 15:00", "gap_type": "none"}],
    )
    assert len(out) == 1  # one spoken request → ONE action, not two
    a = out[0]
    assert a["action_id"] == "live_it"  # the id action.requested already carried
    assert a["requested_live"] is True
    assert a["item"] == IT_LIVE  # the room's own wording wins
    assert a["owner"] == "Ben" and a["deadline"] == "tomorrow 15:00"  # folded in
    # The net consulted the model with BOTH sides of the pair.
    assert len(prompts) == 1
    assert IT_LIVE in prompts[0] and EN_EXTRACTED in prompts[0]


def test_merge_stub_mode_stays_keyfree_but_open(monkeypatch):
    """In stub mode (key-free demo) the net is a no-op: the cross-language pair
    survives as two actions, documented fail-open gap, and NO model is called."""
    monkeypatch.setattr(settings, "brain_provider_post", "stub")

    def boom(*a, **k):  # pragma: no cover; must never run
        raise AssertionError("stub mode must not call the model")

    monkeypatch.setattr(brain.llm, "complete", boom)
    out = main_module._merge_action_items(
        [{"action_id": "live_it", "action": IT_LIVE, "owner": "", "due": ""}],
        [{"item": EN_EXTRACTED}],
    )
    assert len(out) == 2


def test_crosslang_net_is_conservative_and_fails_open(monkeypatch, real_post_provider):
    live = [{"action_id": "l1", "action": "manda il recap a Elena", "due": ""}]
    extracted = [{"item": "Draft the Q3 budget proposal"}]

    # Model says "no match" → both actions kept (distinct asks stay distinct).
    monkeypatch.setattr(brain.llm, "complete", lambda *a, **k: '{"duplicates": []}')
    assert len(main_module._merge_action_items(live, extracted)) == 2

    # Model blows up → fail OPEN: both kept, finalize never breaks.
    def boom(*a, **k):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(brain.llm, "complete", boom)
    assert len(main_module._merge_action_items(live, extracted)) == 2

    # Model returns junk → junk ignored, no crash, both kept.
    monkeypatch.setattr(
        brain.llm,
        "complete",
        lambda *a, **k: '{"duplicates": [{"extracted": 9, "live": 0}, "junk"]}',
    )
    assert len(main_module._merge_action_items(live, extracted)) == 2


def test_semantic_action_duplicates_unit(monkeypatch):
    # Stub mode / empty inputs: no model call, empty result.
    monkeypatch.setattr(settings, "brain_provider_post", "stub")

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("must not call the model")

    monkeypatch.setattr(brain.llm, "complete", boom)
    assert brain.semantic_action_duplicates([IT_LIVE], [EN_EXTRACTED]) == []

    monkeypatch.setattr(settings, "brain_provider_post", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    assert brain.semantic_action_duplicates([], [EN_EXTRACTED]) == []
    assert brain.semantic_action_duplicates([IT_LIVE], []) == []

    # Valid + junk entries mixed: valid pairs survive, junk is skipped.
    monkeypatch.setattr(
        brain.llm,
        "complete",
        lambda *a, **k: (
            '{"duplicates": [{"extracted": 0, "live": 0},'
            ' {"extracted": 7, "live": 0},'
            ' {"extracted": "x", "live": 0},'
            ' "garbage", {"live": 0}]}'
        ),
    )
    assert brain.semantic_action_duplicates([IT_LIVE], [EN_EXTRACTED]) == [(0, 0)]


# ── (a) prevention at the source: the summarizer SEES the live captures ─


def test_post_meeting_prompt_carries_live_actions(monkeypatch, real_post_provider):
    seen: dict = {}

    def fake_complete(system, user, **kw):
        seen["system"], seen["user"] = system, user
        return '{"summary": "s", "decisions": [], "actions": [], "risks": [], "follow_up_email": {}}'

    monkeypatch.setattr(brain.llm, "complete", fake_complete)
    monkeypatch.setattr(brain, "retrieve", lambda avatar, q, k=6: [])
    avatar = avatars.load("laura")

    brain.post_meeting(
        avatar,
        f"Ben: Cedric, {IT_LIVE}",
        live_actions=[{"action": IT_LIVE, "owner": "", "due": "", "action_id": "x1"}],
    )
    # The live capture is in the prompt, under the do-not-re-extract header…
    assert IT_LIVE in seen["user"]
    assert "ALREADY CAPTURED LIVE" in seen["user"]
    # …and the standing rule covers rephrasing AND translation (cross-language).
    assert "ALREADY CAPTURED LIVE" in seen["system"]
    assert "translation" in seen["system"].lower()

    # No live captures → the prompt is unchanged (no empty header block).
    brain.post_meeting(avatar, "Ben: quick sync.", live_actions=[])
    assert "ALREADY CAPTURED LIVE" not in seen["user"]


# ── end-to-end: the production scenario through /sessions/{id}/end ──────


def test_finalize_it_live_vs_en_summarizer_yields_one_action(
    client, recall_stubbed, monkeypatch
):
    """Full finalize path: IT action captured live, summarizer disobeys the
    prevention prompt and re-extracts it in English → the artifact (the source
    of the approval cards) still carries exactly ONE action, on the live id."""
    session = store.create("bot_x", "https://meet.example/x", "cedric")
    session.add_utterance("Ben", f"Cedric, {IT_LIVE}")
    tools.dispatch("queue_action", {"action": IT_LIVE}, session=session)
    live_id = session.queued_actions[0]["action_id"]
    assert live_id

    passed: dict = {}

    def fake_post_meeting(avatar, transcript, **kw):
        passed["live_actions"] = kw.get("live_actions")
        return {  # a summarizer that ignored the instruction (the failure mode)
            "summary": "s",
            "decisions": [],
            "actions": [{"item": EN_EXTRACTED, "owner": "Ben",
                         "deadline": "tomorrow 15:00", "gap_type": "none"}],
            "risks": [],
            "follow_up_email": {},
        }

    net_calls: list = []

    def fake_net(live, extracted):
        net_calls.append((list(live), list(extracted)))
        return [(0, 0)]  # the stubbed model confirms the cross-language match

    monkeypatch.setattr(lifecycle, "post_meeting", fake_post_meeting)
    monkeypatch.setattr(lifecycle, "semantic_action_duplicates", fake_net)

    resp = client.post("/sessions/bot_x/end")
    assert resp.status_code == 200
    actions = resp.json()["actions"]

    assert len(actions) == 1  # ONE approval card, not two
    a = actions[0]
    assert a["action_id"] == live_id  # correlates with the live action.requested
    assert a["requested_live"] is True
    assert a["item"] == IT_LIVE
    assert a["owner"] == "Ben" and a["deadline"] == "tomorrow 15:00"

    # (a) plumbing: the summarizer received the live captures…
    assert [x["action"] for x in passed["live_actions"]] == [IT_LIVE]
    # …and (b) the net compared exactly the live item vs the leftover extraction.
    assert net_calls == [([IT_LIVE], [EN_EXTRACTED])]

    # Every downstream consumer sees the same single action: stored artifact…
    assert len(store.get_artifact("bot_x")["actions"]) == 1
    # …and the cross-meeting ledger (what carryover briefs are built from).
    key = ledger.meeting_key("https://meet.example/x")
    ledger_actions = [i for i in ledger.items(key) if i["kind"] == "action"]
    assert len(ledger_actions) == 1
    assert ledger_actions[0]["item"] == IT_LIVE
