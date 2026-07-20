"""Meeting → browser trigger (Sable B2/B3) — key-free invariants.

Intent detection, dismiss detection, flag gating, and the bridge's
fail-closed / no-transcript-leak behaviour. No pg, no keys, no network — the
success path (real operator session) is in test_browser_operator_pg.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import browser_meeting, decision
from app.config import settings


# ── intent detection ────────────────────────────────────────────────────────

def test_browse_intent_positive_en_and_it():
    yes = [
        "Petra, open Asana and show me my projects",
        "show me Asana",
        "pull up asana on the screen",
        "can you show Asana",
        "mostrami Asana",
        "apri asana e fammi vedere le task",
        "vai su asana",
    ]
    for u in yes:
        ok, site, task = decision.detect_browse_intent(u)
        assert ok and site == "asana" and task == "", u  # plain show


def test_browse_intent_walkthrough_tasks():
    cases = {
        "show me how to change an assignee in asana": "change_assignee",
        "walk me through creating a task in asana": "create_task",
        "come si crea un progetto su asana": "create_project",
        "how do I set a due date in asana": "set_due_date",
        "show me how to add a section in asana": "add_section",
        "teach me how to comment on a task in asana": "add_comment",
        "show me how to use asana": "tour",
    }
    for u, expect in cases.items():
        ok, site, task = decision.detect_browse_intent(u)
        assert ok and site == "asana" and task == expect, (u, task)


def test_browse_intent_negative():
    # Questions to ANSWER (not browse commands) and unrelated asks must not fire.
    no = [
        "what's on the Asana board?",
        "how many tasks are overdue in asana?",
        "who owns the asana migration?",
        "open the door",
        "show me the numbers",
        "what can you do in asana?",
    ]
    for u in no:
        ok, _, _ = decision.detect_browse_intent(u)
        assert not ok, u


def test_browse_dismiss():
    for u in ["close the browser", "hide Asana", "chiudi la vista",
              "get rid of that window", "nascondi il browser"]:
        assert decision.detect_browse_dismiss(u), u
    for u in ["close the deal", "hide nothing here", "what's next"]:
        assert not decision.detect_browse_dismiss(u), u


# ── flag gating + fail-closed bridge ────────────────────────────────────────

def test_trigger_disabled_by_default():
    assert settings.browser_meeting_trigger_enabled is False
    # Even flipping the trigger flag alone is inert without the operator on.
    assert browser_meeting.trigger_enabled() is False


def test_open_unknown_site_fails_closed():
    r = browser_meeting.open_for_meeting(
        "org", avatar_key="petra", site_label="notasite", meeting_ref="m1")
    assert r["ok"] is False and r["reason"] == "unknown_site"


def test_open_swallows_errors_never_raises(monkeypatch):
    # No control plane configured → operator work fails; the bridge must return
    # a graceful {ok: False}, never raise into the meeting turn.
    monkeypatch.setattr(settings, "laura_database_url", "")
    r = browser_meeting.open_for_meeting(
        "org", avatar_key="petra", site_label="asana", meeting_ref="m2")
    assert r["ok"] is False
    assert "url" not in r
    # The spoken fallback names the site, never a payload/utterance.
    assert r["spoken"] == "Asana"


def test_close_for_meeting_noop_when_none():
    assert browser_meeting.close_for_meeting("no-such-meeting") is False


def test_site_spoken_name():
    assert browser_meeting.site_spoken_name("asana") == "Asana"
    assert browser_meeting.site_spoken_name("mystery") == "mystery"



def test_run_walkthrough_unknown_task_fails_closed():
    calls = []
    r = browser_meeting.run_walkthrough(
        "org", "sess", site_label="asana", task_key="nope",
        on_narrate=calls.append)
    assert r["ok"] is False and r["outcome"] == "unknown_task"
    assert calls == []  # nothing spoken on a bad task


def test_run_walkthrough_swallows_errors(monkeypatch):
    # No control plane → coordinator import/run fails; must not raise.
    monkeypatch.setattr(settings, "laura_database_url", "")
    r = browser_meeting.run_walkthrough(
        "org", "sess", site_label="asana", task_key="create_task",
        on_narrate=lambda _l: None)
    assert r["ok"] is False


def test_narration_is_operation_only_no_page_content():
    # Every narration line is a fixed template keyed by operation — never any
    # page/observation text (leak-free by construction).
    for op in ("navigate", "click", "type", "scroll", "read", "mystery"):
        for i in range(3):
            line = browser_meeting._narration_for(op, i)
            assert isinstance(line, str) and line and len(line) < 80



def test_browse_intent_asr_variants_and_surface():
    # ASR mangles "Asana"; strong surface words fall back to the default site.
    for u in ["can you open a sauna for me", "pull up azana",
              "Petra show me on the browser", "open it on screen"]:
        ok, site, _ = decision.detect_browse_intent(u)
        assert ok and site == "asana", u


def test_browse_signal_is_pii_safe_booleans():
    hv, hs = decision.browse_signal("open Asana and show me my projects")
    assert hv is True and hs is True
    hv, hs = decision.browse_signal("open the door")
    assert hv is True and hs is False
    hv, hs = decision.browse_signal("that looks great")
    assert hv is False and hs is False


def test_no_false_fire_on_ordinary_navigation():
    for u in ["go to the next page", "how do I get to the airport",
              "show me the numbers", "open the door"]:
        ok, _, _ = decision.detect_browse_intent(u)
        assert not ok, u
