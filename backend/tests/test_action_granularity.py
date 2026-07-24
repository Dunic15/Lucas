"""Action granularity — live meeting 2026-07-24 (Ananth/Petra transcript).

Seven distinct failures, each reproduced then locked here:
T1 "create a new project called crypto startup" reached approval as a
   create-TASK form (sanitize had no create_project branch → dropped → the
   approve-door synth re-typed it as a task).
T2 "add Duccio as a collaborator on the Asana" became a create-task form —
   no executor supports membership; it must stay an honest untyped note.
T3 "It should go to duccio at SFF studio dot com" glued the leading words
   into the address (itshouldgotoduccio@…).
T4 "Between me and Duccio today?" (a clarify answer) hit the 'today'
   freshness trigger and got a public web answer mid-scheduling.
T5 "today." alone → the identical "when it should be?" re-ask, instead of
   narrowing to "what time?".
T6 capability answer said "no snapshot loaded" while she was reading 6 tasks
   from the workspace brief (asana_live means live TOOLS, not the brief).
T7 "email Duccio the full summary of this meeting" interrogated the human
   for subject/body that already exist in the artifact summary.
Key-free — no vendors.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import capabilities as cap  # noqa: E402
from app.brain import engine, tools  # noqa: E402
from app.config import settings  # noqa: E402


# ── T1: project asks type asana.create_project, never a task ────────────
def test_project_ask_types_create_project_with_the_stated_name():
    out = engine.type_actions(
        [{"item": "So on Asana, I need you to create a new project "
                  "called crypto startup"}],
        provider="stub", allow_asana=True,
    )
    typed = out[0]["typed"]
    assert typed["type"] == "asana.create_project"
    assert typed["args"]["name"] == "crypto startup"


def test_project_without_a_stated_name_still_types_project():
    out = engine.type_actions(
        [{"item": "one more project to add to Asana for the YC application"}],
        provider="stub", allow_asana=True,
    )
    assert out[0]["typed"]["type"] == "asana.create_project"


def test_task_asks_still_type_task_not_project():
    out = engine.type_actions(
        [{"item": "Create an Asana task for the onboarding checklist"}],
        provider="stub", allow_asana=True,
    )
    assert out[0]["typed"]["type"] == "asana.create_task"


def test_typing_prompt_carries_the_project_and_collaborator_rules():
    p = engine.TYPED_ACTION_ASANA
    assert "asana.create_project" in p and "NEVER asana.create_task" in p
    assert "collaborator" in p.lower()


# ── T2: membership asks are never disguised as tasks ────────────────────
def test_collaborator_ask_detection():
    assert tools.is_collaborator_ask(
        "just add Duccio as a collaborator on the Asana, please")
    assert tools.is_collaborator_ask("add Duccio to the project")
    assert not tools.is_collaborator_ask("add a task to the board for Q3")
    assert not tools.is_collaborator_ask("create an Asana task for onboarding")


def test_collaborator_ask_stays_untyped():
    out = engine.type_actions(
        [{"item": "just add Duccio as a collaborator on the Asana, please"}],
        provider="stub", allow_asana=True,
    )
    assert "typed" not in out[0]


# ── T3: spoken email anchored to the address, never the whole phrase ────
@pytest.mark.parametrize("spoken,want", [
    ("It should go to duccio at SFF studio dot com.", "duccio@sffstudio.com"),
    ("duccio at sff studio dot com", "duccio@sffstudio.com"),
    ("duccio dot profeti at gmail dot com", "duccio.profeti@gmail.com"),
    ("duccio at sffstudio.com", "duccio@sffstudio.com"),
    ("send it to Marco", ""),
])
def test_spoken_email_normalization(spoken, want):
    assert tools._exact_email(spoken) == want


# ── T4: scheduling fragments never web-search ────────────────────────────
def test_scheduling_fragments_do_not_search(monkeypatch):
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "synthetic")
    assert engine.wants_web_search("Between me and Duccio today?") is False
    assert engine.wants_web_search("me and Duccio today") is False
    # genuine freshness queries keep searching
    assert engine.wants_web_search("what is the latest AI news today") is True


# ── T5: a partial schedule narrows the next ask ──────────────────────────
def test_day_only_asks_for_the_time():
    m = tools.missing_action_details(
        "create a meeting with dana@acme.com today", "calendar")
    assert m == ["invite_clock"]


def test_time_only_asks_for_the_day():
    m = tools.missing_action_details(
        "create a meeting with dana@acme.com at seven PM", "calendar")
    assert m == ["invite_date"]


def test_full_schedule_is_complete():
    assert tools.missing_action_details(
        "create a meeting with dana@acme.com today at seven PM",
        "calendar") == []


def test_refined_slots_have_spoken_labels():
    from app.actions import action_plane
    assert action_plane.SLOT_LABELS_EN["invite_clock"] == "what time"
    assert action_plane.SLOT_LABELS_EN["invite_date"] == "which day"
    assert "invite_clock" in tools._DETAIL_FOLD_LABELS


# ── T6: the brief, not asana_live, drives the snapshot answer ────────────
class _Sess:
    def __init__(self, live=False, brief=False):
        self.asana_live = live
        self.asana_brief_loaded = brief
        self.tool_registry = {"native": [
            {"name": "asana_tasks", "connected": True, "write": True,
             "verbs": "create task, update task"},
        ]}


def test_brief_loaded_means_snapshot_available(monkeypatch):
    from app.actions import executor
    monkeypatch.setattr(executor, "route_for_typed", lambda t, o: "pipedream")
    snap = cap.snapshot("petra", "o", _Sess(live=False, brief=True))
    t = snap["tools"]["asana_tasks"]
    assert t["snapshot_available_in_meeting"] is True
    assert t["unavailable_reason"] == ""
    a = cap.answer("do you have a snapshot of my asana?", snap)
    assert "don't have a snapshot" not in a.lower()


def test_no_brief_and_no_live_tools_is_still_honest(monkeypatch):
    from app.actions import executor
    monkeypatch.setattr(executor, "route_for_typed", lambda t, o: "pipedream")
    snap = cap.snapshot("petra", "o", _Sess(live=False, brief=False))
    t = snap["tools"]["asana_tasks"]
    assert t["snapshot_available_in_meeting"] is False
    assert "no workspace snapshot" in t["unavailable_reason"]


def test_cache_recomputes_when_the_brief_flag_flips(monkeypatch):
    from app.actions import executor
    monkeypatch.setattr(executor, "route_for_typed", lambda t, o: "pipedream")
    s = _Sess(live=False, brief=False)
    first = cap.cached_snapshot("petra", "o", s)
    assert first["tools"]["asana_tasks"]["snapshot_available_in_meeting"] is False
    s.asana_brief_loaded = True
    second = cap.cached_snapshot("petra", "o", s)
    assert second["tools"]["asana_tasks"]["snapshot_available_in_meeting"] is True


# ── T7: "email the meeting summary" arrives ready to approve ─────────────
def test_summary_email_prefills_subject_and_body():
    summary = "We agreed to ship the YC application by Monday. Duccio owns it."
    acts = engine.prefill_summary_emails(
        [{"item": "Send an email to duccio@sffstudio.com with the full "
                  "summary of this meeting"}],
        summary,
    )
    typed = engine.type_actions(acts, provider="stub")[0]["typed"]
    assert typed["type"] == "email.send"
    assert typed["args"]["to"] == ["duccio@sffstudio.com"]
    assert typed["args"]["subject"] == "Meeting summary"
    assert "YC application" in typed["args"]["body"]


def test_prefill_never_overwrites_a_dictated_body():
    acts = engine.prefill_summary_emails(
        [{"item": "Send an email to duccio@sffstudio.com with the summary "
                  "of this meeting. Body: just say hi"}],
        "A long summary that must not clobber the dictated body.",
    )
    # the dictated Body survives (prefill skipped), so the typed email says hi
    typed = engine.type_actions(acts, provider="stub")[0]["typed"]
    assert typed["args"]["body"] == "just say hi"
    assert "must not clobber" not in acts[0]["item"]


def test_prefill_ignores_unrelated_emails():
    acts = engine.prefill_summary_emails(
        [{"item": "Send an email to marco@acme.com about the invoice"}],
        "Summary text.",
    )
    assert "Subject:" not in acts[0]["item"]
