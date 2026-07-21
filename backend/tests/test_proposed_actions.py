"""Proposed-actions (goals & intents) pipeline: grounding, provenance,
never-auto-execute, and the dashboard projection + transcript pref."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import store  # noqa: E402
from app.brain import engine  # noqa: E402
from app.actions import executor  # noqa: E402
from app.config import settings  # noqa: E402
import app.main as main  # noqa: E402


def _goal_artifact():
    return {
        "actions": [{"item": "apply to internships", "owner": "Ananth"}],
        "goals": [
            {"goal": "start a research project",
             "evidence": "I want to start a research project",
             "proposed_steps": [
                 {"item": "define the research question", "owner": ""},
                 {"item": "draft a roadmap", "owner": "Ananth"},
             ]},
            {"goal": "ungrounded",
             "evidence": "NOT IN THE TRANSCRIPT AT ALL",
             "proposed_steps": [{"item": "bogus step"}]},
        ],
    }


TRANSCRIPT = "so yeah I want to start a research project and apply to things"


def test_absorb_goals_grounds_tags_and_caps():
    art = _goal_artifact()
    engine._absorb_goals(art, TRANSCRIPT)
    items = {a["item"]: a for a in art["actions"]}
    assert items["apply to internships"]["source"] == "explicit"
    step = items["define the research question"]
    assert step["source"] == "inferred"
    assert step["goal"] == "start a research project"
    assert step["inferred_from"] == "I want to start a research project"
    assert step["owner"] == "UNASSIGNED" and step["gap_type"] == "owner"
    assert "bogus step" not in items  # ungrounded goal dropped whole
    assert art["checklist"] == art["actions"]


def test_absorb_goals_caps_steps_per_goal():
    art = {"actions": [], "goals": [{
        "goal": "g", "evidence": "the goal quote",
        "proposed_steps": [{"item": f"s{i}"} for i in range(9)]}]}
    engine._absorb_goals(art, "prefix the goal quote suffix")
    assert len(art["actions"]) == engine._MAX_STEPS_PER_GOAL


def test_auto_execute_skips_inferred(monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(settings, "asana_auto_execute", True)
    ran = []
    monkeypatch.setattr(executor, "execute_approved",
                        lambda org, aid, action: ran.append(aid) or {"ok": True})
    typed = {"type": "asana.create_task", "args": {"name": "x"}}
    actions = [
        {"action_id": "a1", "typed": dict(typed), "source": "explicit"},
        {"action_id": "a2", "typed": dict(typed), "source": "inferred"},
        {"action_id": "a3", "typed": dict(typed)},  # legacy: no source
    ]
    n = executor.auto_execute_asana("org-x", actions)
    assert ran == ["a1", "a3"] and n == 2


def _client():
    return TestClient(main.app)


def test_summary_carries_provenance_and_gates_transcript():
    c = _client()
    store.save_artifact("bot_prov", {
        "summary": "s", "org_id": "",
        "transcript": "the raw words spoken in the meeting",
        "actions": [
            {"action_id": "x1", "item": "explicit thing", "owner": "A",
             "source": "explicit"},
            {"action_id": "x2", "item": "proposed step", "owner": "UNASSIGNED",
             "source": "inferred", "goal": "start a research project",
             "inferred_from": "I want to start a research project"},
        ],
    })
    d = c.get("/dashboard/summary").json()
    m = next(mm for mm in d["meetings"] if mm["bot_id"] == "bot_prov")
    by = {a["item"]: a for a in m["actions"]}
    assert by["explicit thing"]["source"] == "explicit"
    assert by["proposed step"]["source"] == "inferred"
    assert by["proposed step"]["goal"] == "start a research project"
    assert by["proposed step"]["inferred_from"].startswith("I want")
    # transcript pref default OFF -> no transcript key on the wire
    assert "transcript" not in m and d["prefs"]["show_transcripts"] is False
    # flip ON -> transcript present
    r = c.post("/dashboard/prefs/transcripts", json={"enabled": True})
    assert r.status_code == 200
    d2 = c.get("/dashboard/summary").json()
    m2 = next(mm for mm in d2["meetings"] if mm["bot_id"] == "bot_prov")
    assert m2["transcript"] == "the raw words spoken in the meeting"
    assert d2["prefs"]["show_transcripts"] is True
    # and back OFF
    c.post("/dashboard/prefs/transcripts", json={"enabled": False})
    d3 = c.get("/dashboard/summary").json()
    m3 = next(mm for mm in d3["meetings"] if mm["bot_id"] == "bot_prov")
    assert "transcript" not in m3


def test_total_wipe_triggers_one_verbatim_retry(monkeypatch):
    """Model extracts asks but grounding kills all of them -> exactly one retry
    with the verbatim reminder; the retry's surviving actions win."""
    import json
    from app import avatars
    from app.brain import engine as brain

    transcript = "Petra, please create a task in Asana for the connectivity check"
    bad = {"summary": "s", "decisions": [], "risks": [], "follow_up_email": {},
           "actions": [{"item": "Organize the quarterly offsite in Lisbon",
                        "owner": "X", "deadline": "",
                        "evidence": "they talked about an offsite event"}]}
    good = {"summary": "s", "decisions": [], "risks": [], "follow_up_email": {},
            "actions": [{"item": "Create a task in Asana", "owner": "Petra",
                         "deadline": "", "gap_type": "none",
                         "evidence": "please create a task in Asana for the connectivity check"}]}
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append(system)
        return json.dumps(bad if len(calls) == 1 else good)

    monkeypatch.setattr(settings, "brain_provider_post", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(brain.llm, "complete", fake_complete)
    monkeypatch.setattr(brain, "retrieve", lambda avatar, query, k=6: [])

    artifact = brain.post_meeting(avatars.load("petra"), transcript)

    assert len(calls) == 2
    assert "EXACTLY, character-for-character" in calls[1]
    assert [a["item"] for a in artifact["actions"]] == ["Create a task in Asana"]


def test_no_retry_when_actions_survive(monkeypatch):
    import json
    from app import avatars
    from app.brain import engine as brain

    transcript = "Petra, please create a task in Asana for the connectivity check"
    good = {"summary": "s", "decisions": [], "risks": [], "follow_up_email": {},
            "actions": [{"item": "Create a task in Asana", "owner": "Petra",
                         "deadline": "", "gap_type": "none",
                         "evidence": "make an Asana task for the connectivity check"}]}
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append(1)
        return json.dumps(good)

    monkeypatch.setattr(settings, "brain_provider_post", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(brain.llm, "complete", fake_complete)
    monkeypatch.setattr(brain, "retrieve", lambda avatar, query, k=6: [])

    artifact = brain.post_meeting(avatars.load("petra"), transcript)
    assert len(calls) == 1  # fuzzy grounding accepted the light paraphrase
    assert [a["item"] for a in artifact["actions"]] == ["Create a task in Asana"]
