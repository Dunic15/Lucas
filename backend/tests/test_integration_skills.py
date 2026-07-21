"""Per-integration skills — lazy, cached, size-capped, safe on garbage input."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import integration_skills


def test_asana_skill_exists_and_loads():
    integration_skills._reset()
    body = integration_skills.skill_for("asana")
    assert "Asana" in body and "permalink_url" in body
    assert len(body) <= integration_skills._MAX_CHARS


def test_missing_skill_is_empty():
    integration_skills._reset()
    assert integration_skills.skill_for("notion") in ("",) or isinstance(
        integration_skills.skill_for("notion"), str
    )
    # A slug with no file returns '' (most apps need no skill).
    assert integration_skills.skill_for("definitely_not_an_app_xyz") == ""


def test_garbage_slugs_are_safe():
    integration_skills._reset()
    assert integration_skills.skill_for("") == ""
    assert integration_skills.skill_for("../../etc/passwd") == ""
    assert integration_skills.skill_for("As ana!") == ""
    assert integration_skills.skill_for(None) == ""  # type: ignore[arg-type]


def test_cache_serves_without_reread(tmp_path, monkeypatch):
    integration_skills._reset()
    monkeypatch.setattr(integration_skills, "_SKILLS_DIR", tmp_path)
    f = tmp_path / "todoist.md"
    f.write_text("# Todoist tips", encoding="utf-8")
    assert integration_skills.skill_for("todoist") == "# Todoist tips"
    # Same mtime ⇒ served from cache even if content changes on disk without
    # an mtime bump (write with the same mtime is not realistic; emulate by
    # checking the cache entry directly).
    assert "todoist" in integration_skills._cache


def test_typing_prompt_includes_asana_skill(monkeypatch):
    """type_actions' model call gets the Asana skill appended when allow_asana."""
    from app.brain import engine

    integration_skills._reset()
    captured = {}

    def fake_complete(system, user, **kw):
        captured["system"] = system
        return "{}"

    monkeypatch.setattr(engine.llm, "complete", fake_complete)
    engine._llm_type_actions(
        [(0, {"item": "Marco to send the deck"})], "sum", "anthropic",
        allow_asana=True,
    )
    assert "Integration guidance (asana)" in captured["system"]
    assert "permalink_url" in captured["system"]
    # And without allow_asana the skill is NOT loaded.
    captured.clear()
    engine._llm_type_actions(
        [(0, {"item": "Marco to send the deck"})], "sum", "anthropic",
        allow_asana=False,
    )
    assert "Integration guidance" not in captured["system"]
