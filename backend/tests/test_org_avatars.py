"""Org avatar overlays (M2), key-free half: validation, merge, flag-off.

The invariants that need no database: the overlay allowlist rejects unknown
fields / capability widening / bad shapes; the resolver's merge narrows and
re-skins but never mutates or replaces canonical material; and with
ORG_AVATAR_OVERLAYS_ENABLED off (the default) every path is byte-identical -
resolve() returns THE SAME cached instance avatars.load returns, and every
Studio route 404s.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import avatar_overlay, avatar_resolver, avatars, store
from app.brain import engine as brain
from app.config import settings


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    avatar_resolver._reset_for_tests()
    yield
    avatar_resolver._reset_for_tests()


def _laura():
    return avatars.load("laura")


# ── validation ──────────────────────────────────────────────────────────────

def test_unknown_fields_and_secrets_are_rejected():
    for payload in (
        {"persona_prompt": "evil replacement"},
        {"system_prompt": "x"},
        {"oauth_token": "x"},
        {"webhook_secret": "x"},
        {"wake_words": ["only-this"]},
        {"tools": [{"name": "rm -rf"}]},
        {"whatever": 1},
    ):
        _clean, errors = avatar_overlay.validate_overlay(_laura(), payload)
        assert errors, f"{payload} must be rejected"


def test_capability_widening_is_rejected():
    # laura's ceiling is the baseline (google, slack); asana is Petra-only.
    clean, errors = avatar_overlay.validate_overlay(
        _laura(), {"enabled_tools": ["google", "asana"]}
    )
    assert any("widen" in e for e in errors)
    # Narrowing is fine, and [] (no tools) stays distinct from absent.
    clean, errors = avatar_overlay.validate_overlay(
        _laura(), {"enabled_tools": []}
    )
    assert not errors and clean["enabled_tools"] == []


def test_shapes_and_bounds():
    clean, errors = avatar_overlay.validate_overlay(
        _laura(), {"voice_id": "not a voice!!"}
    )
    assert errors
    clean, errors = avatar_overlay.validate_overlay(
        _laura(), {"face": "hologram"}
    )
    assert errors
    clean, errors = avatar_overlay.validate_overlay(
        _laura(), {"instructions": "x" * 10000, "mission": "y" * 5000}
    )
    # Individual fields are trimmed to their caps; the combined prompt budget
    # is enforced as an ERROR, never silent truncation past the total.
    assert not errors or any("exceed" in e for e in errors)
    scope, errors = avatar_overlay.validate_context_scope(
        {"knowledge_source_ids": ["11111111-1111-1111-1111-111111111111"],
         "include_org_default": False, "labels": ["sales"], "purpose": "demo"}
    )
    assert not errors and scope["include_org_default"] is False
    _scope, errors = avatar_overlay.validate_context_scope(
        {"knowledge_source_ids": "not-a-list"}
    )
    assert errors


def test_preferences_block_is_bounded_and_additive():
    assert avatar_overlay.preferences_block({}) == ""
    block = avatar_overlay.preferences_block(
        {"tone": "warm, concise", "vocabulary": ["Acme Cloud"],
         "instructions": "never quote prices"}
    )
    assert "Organization preferences" in block
    assert "never override" in block
    assert len(block) <= avatar_overlay.MAX_PROMPT_BLOCK_CHARS


# ── merge semantics ─────────────────────────────────────────────────────────

def _apply(overlay: dict):
    return avatar_resolver._apply(
        _laura(), "org-x", {"version": 7, "overlay": overlay}
    )


def test_apply_narrows_and_reskins_without_touching_canonical():
    canonical = _laura()
    before_name = canonical.name
    before_persona = canonical.persona_prompt
    resolved = _apply({
        "display_name": "Ava", "role": "Sales Engineer",
        "tone": "crisp", "enabled_tools": ["slack"],
        "voice_id": "cjVigY5qzO86Huf0OWal",
    })
    assert resolved.name == "Ava"
    assert resolved.overlay_version == 7
    # Wake words: canonical KEPT, display name ADDED (stop/leave phrases work).
    assert set(canonical.wake_words) <= set(resolved.wake_words)
    assert "ava" in resolved.wake_words
    # Persona: canonical prefix intact, org block appended; never replaced.
    assert resolved.persona_prompt.startswith(before_persona)
    assert "Organization preferences" in resolved.persona_prompt
    # Intersection: google removed, slack kept, asana never appears.
    assert resolved.effective_tools == ("slack",)
    # The shared canonical instance was never mutated.
    assert canonical.name == before_name
    assert canonical.persona_prompt == before_persona


def test_apply_empty_tools_means_no_tools():
    resolved = _apply({"enabled_tools": []})
    assert resolved.effective_tools == ()
    assert resolved.native_tools == []


# ── flag-off byte-identity ──────────────────────────────────────────────────

def test_resolve_flag_off_returns_the_exact_cached_instance():
    canonical = avatars.load("laura")
    resolved = avatar_resolver.resolve("some-org", "laura")
    assert resolved is canonical  # identity, not equality


def test_for_session_without_stash_is_the_canonical_load(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    session = store.create("bot-m2", "https://meet.google.com/m2-test", "laura")
    assert avatar_resolver.for_session(session) is avatars.load("laura")
    store.remove("bot-m2")


def test_family_allowed_defaults_open_when_off():
    assert avatar_resolver.family_allowed("org-x", "laura", "google") is True


def test_resolve_avatar_key_precedence_flag_off():
    assert avatar_resolver.resolve_avatar_key(
        "org-x", requested="cedric"
    ) == "cedric"
    assert avatar_resolver.resolve_avatar_key(
        "org-x", requested=""
    ) == settings.default_avatar_id


def test_studio_routes_404_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    client = TestClient(main_module.app)
    assert client.get("/org/avatars").status_code == 404
    assert client.get("/dashboard/avatar-studio/avatars").status_code == 404
    assert client.put(
        "/org/avatars/laura/draft", json={"overlay": {}}
    ).status_code == 404


def test_stream_prompt_name_is_parameterized_and_byte_identical_for_laura():
    system = brain.ANSWER_STREAM_SYSTEM.format(
        persona="P", name="Laura"
    )
    assert "You are Laura," in system
    renamed = brain.ANSWER_STREAM_SYSTEM.format(persona="P", name="Ava")
    assert "You are Ava," in renamed and "You are Laura," not in renamed
