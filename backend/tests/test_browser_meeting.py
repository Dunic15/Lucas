"""Meeting → browser trigger (Sable B2/B3); key-free invariants.

Intent detection, dismiss detection, flag gating, and the bridge's
fail-closed / no-transcript-leak behaviour. No pg, no keys, no network; the
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
        "show me how to assign a task in asana": "assign_task",
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


def test_narration_generic_and_named():
    # Back-compat: a bare operation string yields a generic line.
    for op in ("navigate", "click", "type", "scroll", "read", "mystery"):
        for i in range(3):
            line = browser_meeting._narration_for(op, i)
            assert isinstance(line, str) and line and len(line) < 90
            assert "{name}" not in line  # template must be filled/omitted
    # A named control produces a line that voices the (bounded) label.
    named = browser_meeting._narration_for(
        {"operation": "click", "name": "Add task"}, 0)
    assert "Add task" in named and "{name}" not in named
    # A named-but-empty falls back to a generic line, no dangling placeholder.
    empty = browser_meeting._narration_for({"operation": "click", "name": ""}, 1)
    assert "{name}" not in empty and empty



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


def test_walkthrough_cancel_stops_immediately(monkeypatch):
    # A cancel() that returns True must stop the coordinator before any
    # narration/action; the barge-in contract for walkthroughs.
    from app import browser
    from app.browser import coordinator
    calls = []
    monkeypatch.setattr(browser, "enabled", lambda: True)
    monkeypatch.setattr(settings, "browser_visual_planner_enabled", True)

    class _Planner:  # would propose forever if not cancelled
        def propose(self, *a, **k):
            calls.append("propose")
            return {"operation": "read"}

    r = coordinator.run("org", "sess", "goal", planner=_Planner(),
                        cancel=lambda: True)
    assert r["outcome"] == "cancelled"
    assert calls == []  # cancelled before any model call


def test_walkthrough_cancelled_closing_is_silent():
    assert browser_meeting._CLOSING["cancelled"] == ""


# ── scripted recipes (reliable how-to) ──────────────────────────────────────

def test_recipe_exists_for_asana_tasks():
    from app.browser import recipes
    for task in ("create_task", "create_project", "tour"):
        steps = recipes.recipe_for("asana", task)
        assert steps and all("op" in s and "say" in s for s in steps), task
    assert recipes.recipe_for("asana", "nope") is None
    assert recipes.recipe_for("mystery", "create_task") is None


def test_recipe_selector_alternates():
    from app.browser import recipes
    alts = recipes.selector_alternates("text=A || [aria-label='B'] || c")
    assert alts == ["text=A", "[aria-label='B']", "c"]
    assert recipes.selector_alternates("") == []

