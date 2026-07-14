"""Per-meeting MISSION + deterministic TASK HINTS.

Two additive, off-by-default upgrades:

  1. MISSION — an admin objective the avatar keeps in mind and RESURFACES if
     unmet. It is folded into the live-answer and closing (proactive) system
     prompts as an instruction only; it never gates when she speaks.
  2. TASK HINTS — recognizable asks (avatar.yaml `tasks`) that BIAS post-meeting
     action capture toward typed actions, without fabricating anything.

Key-free like the rest of the suite: no vendors are called — the brain is forced
onto a fake `llm` seam that just captures the assembled prompt, and retrieval is
stubbed. The whole point is that an EMPTY mission / EMPTY tasks leave the prompt
byte-identical to today.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars, brain  # noqa: E402
from app.avatars import Avatar, _normalize_tasks  # noqa: E402
from app.cedric import integration as cedric  # noqa: E402
from app.config import settings  # noqa: E402
from app.rag import Retrieved  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────
def _avatar(**overrides) -> Avatar:
    base = dict(
        id="laura",
        name="Laura",
        role="AI Process Expert",
        wake_words=["laura"],
        persona_prompt="",
        anam_avatar_id="r1",
        elevenlabs_voice_id="v1",
        min_confidence=0.55,
        speak_cooldown_seconds=8.0,
        dir=Path("."),
    )
    base.update(overrides)
    return Avatar(**base)


def _chunk() -> list[Retrieved]:
    return [
        Retrieved(
            text="Provisioning access requires manager approval before IT acts.",
            source="access_security_sop.md",
            section="Provisioning",
            score=0.91,
        )
    ]


MISSION = "On investor calls, make sure market size is discussed."


# ── the pure prompt-builders ────────────────────────────────────────────────
def test_mission_directive_empty_is_noop():
    assert brain._mission_directive("") == ""
    assert brain._mission_directive("   ") == ""
    assert brain._mission_directive(None) == ""


def test_mission_directive_carries_the_objective():
    out = brain._mission_directive(MISSION)
    assert MISSION in out
    assert "MISSION FOR THIS MEETING" in out
    # It is an instruction to raise-if-unmet, never a barge-in.
    assert "Never interrupt" in out


def test_task_hints_block_empty_is_noop():
    assert brain._task_hints_block(None) == ""
    assert brain._task_hints_block([]) == ""
    # trigger-less / malformed entries produce no block
    assert brain._task_hints_block([{"name": "x"}, "notadict"]) == ""


def test_task_hints_block_lists_triggers_and_action():
    block = brain._task_hints_block(
        [
            {
                "name": "send_recap_email",
                "triggers": ["send the recap", "email the summary"],
                "action": "Draft and send a recap email",
            }
        ]
    )
    assert "KNOWN TASK TYPES" in block
    assert "Draft and send a recap email" in block
    assert '"send the recap"' in block and '"email the summary"' in block
    # It must not license fabrication — the evidence rule is restated.
    assert "Do NOT invent" in block


# ── MISSION injected into the LIVE answer prompt ────────────────────────────
def _force_streaming(monkeypatch, captured: dict) -> None:
    monkeypatch.setattr(brain.settings, "brain_provider", "anthropic")
    monkeypatch.setattr(brain.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: _chunk())
    # Skip the web-search / deep-thought router — force the plain fast path.
    monkeypatch.setattr(brain, "_live_route", lambda q: ("anthropic", "m"))

    def fake_stream(system, user, *a, **k):
        captured["system"] = system
        captured["user"] = user
        return iter(["Sure, here is the answer."])

    monkeypatch.setattr(brain.llm, "stream_complete", fake_stream)


def test_mission_is_injected_into_live_answer_when_set(monkeypatch):
    captured: dict = {}
    _force_streaming(monkeypatch, captured)
    out = list(
        brain.answer_question_stream(_avatar(), "What is the access flow?", mission=MISSION)
    )
    assert out  # she answered
    assert MISSION in captured["system"]
    assert "MISSION FOR THIS MEETING" in captured["system"]


def test_no_mission_leaves_live_answer_prompt_unchanged(monkeypatch):
    captured: dict = {}
    _force_streaming(monkeypatch, captured)
    list(brain.answer_question_stream(_avatar(), "What is the access flow?"))
    assert "MISSION FOR THIS MEETING" not in captured["system"]


# ── MISSION injected into the CLOSING (proactive) prompt ────────────────────
def _force_proactive(monkeypatch, captured: dict) -> None:
    monkeypatch.setattr(brain.settings, "brain_provider", "anthropic")
    monkeypatch.setattr(brain.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(brain, "retrieve", lambda *a, **k: [])

    def fake_complete(system, user, *a, **k):
        captured["system"] = system
        return '{"should_speak": false, "line": "", "confidence": 0.0}'

    monkeypatch.setattr(brain.llm, "complete", fake_complete)


def test_mission_is_injected_into_proactive_when_set(monkeypatch):
    captured: dict = {}
    _force_proactive(monkeypatch, captured)
    # state=None → no deterministic short-circuit, so the model path runs.
    brain.proactive_flag(_avatar(), "some meeting text", state=None, mission=MISSION)
    assert MISSION in captured["system"]


def test_no_mission_leaves_proactive_prompt_unchanged(monkeypatch):
    captured: dict = {}
    _force_proactive(monkeypatch, captured)
    brain.proactive_flag(_avatar(), "some meeting text", state=None)
    assert "MISSION FOR THIS MEETING" not in captured["system"]


# ── TASK HINTS bias the post-meeting extraction prompt ──────────────────────
def _force_post(monkeypatch, captured: dict) -> None:
    monkeypatch.setattr(settings, "brain_provider_post", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(brain, "retrieve", lambda *a, **k: [])

    def fake_complete(system, user, *a, **k):
        captured["user"] = user
        return '{"summary": "ok", "decisions": [], "actions": [], "risks": [], "follow_up_email": {}}'

    monkeypatch.setattr(brain.llm, "complete", fake_complete)


TASKS = [
    {
        "name": "send_recap_email",
        "triggers": ["send the recap", "email the summary"],
        "action": "Draft and send a recap email",
    }
]


def test_tasks_bias_the_extraction_prompt_when_set(monkeypatch):
    captured: dict = {}
    _force_post(monkeypatch, captured)
    avatar = dataclasses.replace(avatars.load("laura"), tasks=TASKS)
    transcript = "Ben: Please send the recap to Marco by Friday.\n"
    brain.post_meeting(avatar, transcript)
    assert "KNOWN TASK TYPES" in captured["user"]
    assert "Draft and send a recap email" in captured["user"]
    assert '"send the recap"' in captured["user"]


def test_no_tasks_leaves_extraction_prompt_unchanged(monkeypatch):
    captured: dict = {}
    _force_post(monkeypatch, captured)
    avatar = dataclasses.replace(avatars.load("laura"), tasks=[])
    brain.post_meeting(avatar, "Ben: Please send the recap.\n")
    assert "KNOWN TASK TYPES" not in captured["user"]


# ── avatar.yaml loading ─────────────────────────────────────────────────────
def test_normalize_tasks_drops_malformed_and_coerces():
    assert _normalize_tasks(None) == []
    assert _normalize_tasks([]) == []
    out = _normalize_tasks(
        [
            {"name": "a", "action": "do a"},                     # no triggers -> dropped
            {"triggers": ["x"], "action": "do x"},               # no name -> name from action
            {"name": "c", "triggers": "single", "action": ""},   # str trigger; action from name
            "notadict",                                          # ignored
        ]
    )
    assert [t["name"] for t in out] == ["do x", "c"]
    assert out[0]["triggers"] == ["x"]
    assert out[1]["triggers"] == ["single"]
    assert out[1]["action"] == "c"  # action falls back to the name


def _point_avatars_dir(monkeypatch, tmp_path) -> Path:
    """settings.avatars_dir is a read-only computed property (REPO_ROOT/avatars);
    repoint it at a temp tree by patching REPO_ROOT, then clear the mtime cache."""
    from app import config

    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    avatars._load_cache.clear()
    root = tmp_path / "avatars"
    root.mkdir(exist_ok=True)
    return root


def test_avatar_yaml_loads_mission_and_tasks(tmp_path, monkeypatch):
    root = _point_avatars_dir(monkeypatch, tmp_path)
    d = root / "vic"
    (d / "knowledge").mkdir(parents=True)
    (d / "avatar.yaml").write_text(
        "id: vic\n"
        "name: Vic\n"
        "role: Analyst\n"
        "wake_words:\n  - vic\n"
        "persona_prompt: hi\n"
        "mission: >\n  Make sure market size is covered.\n"
        "tasks:\n"
        '  - name: send_recap_email\n'
        '    triggers: ["send the recap", "email the summary"]\n'
        "    action: Draft and send a recap email\n"
    )
    a = avatars.load("vic")
    assert "market size" in a.mission
    assert a.tasks and a.tasks[0]["name"] == "send_recap_email"
    assert a.tasks[0]["triggers"] == ["send the recap", "email the summary"]
    assert a.tasks[0]["action"] == "Draft and send a recap email"


def test_avatar_without_mission_or_tasks_is_backward_compatible(tmp_path, monkeypatch):
    root = _point_avatars_dir(monkeypatch, tmp_path)
    d = root / "min"
    (d / "knowledge").mkdir(parents=True)
    (d / "avatar.yaml").write_text(
        "id: min\nname: Min\nrole: r\nwake_words:\n  - min\npersona_prompt: hi\n"
    )
    a = avatars.load("min")
    assert a.mission == ""
    assert a.tasks == []


# ── MeetingContext / integration wiring ─────────────────────────────────────
def test_meeting_context_carries_mission():
    ctx = cedric.MeetingContext(mission="Raise market size", meeting={"title": "Call"})
    assert ctx.mission == "Raise market size"
    # default stays empty (backward compatible)
    assert cedric.MeetingContext().mission == ""


def test_build_integration_carries_mission():
    import types

    ctx = cedric.MeetingContext(mission="Raise market size")
    req = types.SimpleNamespace(
        callback_url="https://cb.example/hook",
        context_url=None,
        external_ref={"team": "T1"},
        context=ctx,
    )
    integ = cedric.build_integration(req, brief="")
    assert integ is not None
    assert integ["mission"] == "Raise market size"


def test_resolve_mission_reads_session_integration():
    import types

    sess = types.SimpleNamespace(integration={"mission": "Raise market size"})
    assert cedric.resolve_mission(sess) == "Raise market size"
    # no mission / no integration / no session → "" (falls back to avatar default)
    assert cedric.resolve_mission(types.SimpleNamespace(integration={})) == ""
    assert cedric.resolve_mission(types.SimpleNamespace(integration=None)) == ""
    assert cedric.resolve_mission(None) == ""
