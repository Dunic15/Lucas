"""Cedric-only ElevenLabs Agents runtime — PR 1 isolation matrix.

The pilot's safety property is that FOUR independent conditions must all
agree before an avatar leaves the legacy pipeline (global flag, allowlist,
avatar.yaml opt-in, non-empty agent id) — so no single configuration mistake
can ever move Laura, Petra, or any other avatar onto the ElevenLabs runtime.
Covers: the shipped yaml state, loader normalization, the resolver matrix,
the session snapshot (frozen at store.create), and the restart fallback.
"""
from __future__ import annotations

import dataclasses
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app import avatars, ledger, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.integrations import elevenlabs_agent  # noqa: E402


def _bare_avatar(**overrides) -> avatars.Avatar:
    base = dict(
        id="x", name="X", role="", wake_words=["x"], persona_prompt="",
        anam_avatar_id="", elevenlabs_voice_id="", min_confidence=0.5,
        speak_cooldown_seconds=8.0, dir=Path("."),
    )
    base.update(overrides)
    return avatars.Avatar(**base)


def _cedric_like(**overrides) -> avatars.Avatar:
    fields = dict(
        id="cedric",
        conversation_runtime="elevenlabs_agent",
        elevenlabs_agent_id="agent_test_123",
    )
    fields.update(overrides)
    return _bare_avatar(**fields)


def _point_avatars_dir(monkeypatch, tmp_path) -> Path:
    """settings.avatars_dir is a read-only computed property (REPO_ROOT/avatars);
    repoint it at a temp tree by patching REPO_ROOT (test_mission_and_tasks
    pattern), then clear the mtime cache."""
    from app import config

    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    avatars._load_cache.clear()
    root = tmp_path / "avatars"
    root.mkdir(exist_ok=True)
    return root


def _write_avatar_yaml(root: Path, avatar_id: str, body: str) -> None:
    folder = root / avatar_id
    folder.mkdir(parents=True)
    (folder / "avatar.yaml").write_text(body)


# ── shipped yaml state ────────────────────────────────────────────────


def test_cedric_yaml_opts_in_but_stays_inert():
    """Cedric's shipped yaml selects the runtime — with an EMPTY agent id, so
    even a flipped global flag alone cannot dispatch ElevenLabs yet."""
    cedric = avatars.load("cedric")
    assert cedric.conversation_runtime == "elevenlabs_agent"
    assert cedric.elevenlabs_agent_id == ""
    assert cedric.voice_multiparty_mode == "wake_word_gate"
    assert cedric.voice_actions_mode == "prepare_only"


def test_laura_and_petra_yaml_stay_legacy():
    for aid in ("laura", "petra"):
        a = avatars.load(aid)
        assert a.conversation_runtime == "legacy", aid
        assert a.elevenlabs_agent_id == "", aid
        assert a.voice_multiparty_mode == "off", aid
        assert a.voice_actions_mode == "off", aid


def test_every_installed_avatar_except_cedric_is_legacy():
    for aid in avatars.list_ids():
        if aid == "cedric":
            continue
        assert avatars.load(aid).conversation_runtime == "legacy", aid


# ── loader normalization ──────────────────────────────────────────────


def test_avatar_without_fields_defaults_legacy(tmp_path, monkeypatch):
    root = _point_avatars_dir(monkeypatch, tmp_path)
    _write_avatar_yaml(root, "plain", "id: plain\nname: Plain\n")
    a = avatars.load("plain")
    assert a.conversation_runtime == "legacy"
    assert a.elevenlabs_agent_id == ""
    assert a.voice_multiparty_mode == "off"
    assert a.voice_actions_mode == "off"


def test_unknown_yaml_values_normalize_to_safe_defaults(tmp_path, monkeypatch):
    """A typo'd runtime/mode can never select something that doesn't exist."""
    root = _point_avatars_dir(monkeypatch, tmp_path)
    _write_avatar_yaml(
        root,
        "typo",
        "id: typo\nconversation_runtime: eleven_labs\n"
        "voice_multiparty_mode: wakeword\nvoice_actions_mode: write_everything\n",
    )
    a = avatars.load("typo")
    assert a.conversation_runtime == "legacy"
    assert a.voice_multiparty_mode == "off"
    assert a.voice_actions_mode == "off"


def test_loader_case_and_whitespace_insensitive(tmp_path, monkeypatch):
    root = _point_avatars_dir(monkeypatch, tmp_path)
    _write_avatar_yaml(
        root,
        "spacey",
        "id: spacey\nconversation_runtime: '  ELEVENLABS_AGENT '\n"
        "elevenlabs_agent_id: '  agent_abc  '\n",
    )
    a = avatars.load("spacey")
    assert a.conversation_runtime == "elevenlabs_agent"
    assert a.elevenlabs_agent_id == "agent_abc"


# ── resolver matrix (the four-condition AND) ──────────────────────────


def test_flag_off_forces_legacy_even_when_fully_configured():
    """The shipped default: flag OFF (pinned by conftest) => always legacy."""
    assert settings.elevenlabs_agent_runtime_enabled is False
    assert elevenlabs_agent.runtime_for_avatar(_cedric_like()) == "legacy"


def test_all_four_conditions_met_resolves_elevenlabs(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    assert (
        elevenlabs_agent.runtime_for_avatar(_cedric_like()) == "elevenlabs_agent"
    )


def test_avatar_outside_allowlist_stays_legacy(monkeypatch):
    """A copy-pasted yaml block on Petra still resolves legacy: not allowlisted."""
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    petra = _cedric_like(id="petra")
    assert elevenlabs_agent.runtime_for_avatar(petra) == "legacy"


def test_empty_agent_id_blocks_dispatch(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    assert (
        elevenlabs_agent.runtime_for_avatar(_cedric_like(elevenlabs_agent_id=""))
        == "legacy"
    )
    assert (
        elevenlabs_agent.runtime_for_avatar(_cedric_like(elevenlabs_agent_id="   "))
        == "legacy"
    )


def test_yaml_legacy_wins_even_when_flag_and_allowlist_agree(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    cedric_legacy = _cedric_like(conversation_runtime="legacy")
    assert elevenlabs_agent.runtime_for_avatar(cedric_legacy) == "legacy"


def test_empty_allowlist_disables_everyone(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    monkeypatch.setattr(settings, "elevenlabs_agent_avatar_allowlist", "")
    assert elevenlabs_agent.runtime_for_avatar(_cedric_like()) == "legacy"


def test_allowlist_parses_csv_case_insensitive(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    monkeypatch.setattr(
        settings, "elevenlabs_agent_avatar_allowlist", " Cedric , OTHER "
    )
    assert elevenlabs_agent.runtime_for_avatar(_cedric_like()) == "elevenlabs_agent"
    assert (
        elevenlabs_agent.runtime_for_avatar(
            _cedric_like(id="other", conversation_runtime="elevenlabs_agent")
        )
        == "elevenlabs_agent"
    )


def test_resolver_never_raises(monkeypatch):
    """A resolver crash must fail closed to legacy, never block dispatch."""
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    assert elevenlabs_agent.runtime_for_avatar(None) == "legacy"
    assert elevenlabs_agent.runtime_for_avatar(object()) == "legacy"


def test_runtime_for_avatar_id_unknown_avatar_is_legacy(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    assert elevenlabs_agent.runtime_for_avatar_id("no-such-avatar") == ("legacy", "")


def test_runtime_for_avatar_id_resolves_configured_avatar(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    root = _point_avatars_dir(monkeypatch, tmp_path)
    _write_avatar_yaml(
        root,
        "cedric",
        "id: cedric\nconversation_runtime: elevenlabs_agent\n"
        "elevenlabs_agent_id: agent_pilot_1\n",
    )
    assert elevenlabs_agent.runtime_for_avatar_id("cedric") == (
        "elevenlabs_agent",
        "agent_pilot_1",
    )
    # The flag off again: same yaml, resolution collapses to legacy.
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", False)
    assert elevenlabs_agent.runtime_for_avatar_id("cedric") == ("legacy", "")


# ── session snapshot (frozen at store.create) ─────────────────────────


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Clean, isolated sqlite store (pattern from test_meter_safety)."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    yield


def test_store_create_stamps_legacy_by_default(fresh_store):
    s = store.create("bot_rt_1", "https://meet.example/rt1", "cedric")
    assert s.conversation_runtime == "legacy"
    assert s.elevenlabs_agent_id == ""
    store.remove("bot_rt_1")


def test_store_create_stamps_elevenlabs_snapshot(fresh_store, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    root = _point_avatars_dir(monkeypatch, tmp_path)
    _write_avatar_yaml(
        root,
        "cedric",
        "id: cedric\nconversation_runtime: elevenlabs_agent\n"
        "elevenlabs_agent_id: agent_pilot_1\n",
    )
    s = store.create("bot_rt_2", "https://meet.example/rt2", "cedric")
    assert s.conversation_runtime == "elevenlabs_agent"
    assert s.elevenlabs_agent_id == "agent_pilot_1"

    # FROZEN: flipping config after creation never migrates the live session.
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", False)
    assert s.conversation_runtime == "elevenlabs_agent"

    # A sibling avatar dispatched under the same (flipped-on) config stays
    # legacy: not allowlisted, no yaml opt-in.
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    _write_avatar_yaml(root, "laura", "id: laura\n")
    other = store.create("bot_rt_3", "https://meet.example/rt3", "laura")
    assert other.conversation_runtime == "legacy"
    store.remove("bot_rt_2")
    store.remove("bot_rt_3")


def test_restart_rehydration_falls_back_to_legacy(fresh_store, tmp_path, monkeypatch):
    """After a mid-meeting backend restart the relay bridge is gone with the
    process — the rehydrated session must run legacy, by design."""
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    root = _point_avatars_dir(monkeypatch, tmp_path)
    _write_avatar_yaml(
        root,
        "cedric",
        "id: cedric\nconversation_runtime: elevenlabs_agent\n"
        "elevenlabs_agent_id: agent_pilot_1\n",
    )
    s = store.create("bot_rt_4", "https://meet.example/rt4", "cedric")
    assert s.conversation_runtime == "elevenlabs_agent"
    store._load_from_db()  # simulate a process restart
    reloaded = store.get("bot_rt_4")
    assert reloaded is not None
    assert reloaded.conversation_runtime == "legacy"
    assert reloaded.elevenlabs_agent_id == ""
    store.remove("bot_rt_4")


def test_snapshot_fields_are_not_persisted_columns(fresh_store):
    """The snapshot is in-memory ON PURPOSE (restart => legacy). Guard against
    someone 'helpfully' persisting it later without revisiting the fallback
    design — the sessions table must not grow these columns silently."""
    s = store.create("bot_rt_5", "https://meet.example/rt5", "laura")
    assert s is not None
    with store._connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "conversation_runtime" not in cols
    assert "elevenlabs_agent_id" not in cols
    store.remove("bot_rt_5")


# ── config surface ────────────────────────────────────────────────────


def test_flag_defaults_are_safe():
    from app.core.config import Settings

    assert Settings.model_fields["elevenlabs_agent_runtime_enabled"].default is False
    assert (
        Settings.model_fields["elevenlabs_agent_avatar_allowlist"].default == "cedric"
    )


def test_dataclass_replace_keeps_runtime_fields():
    """conftest's wake-word normalization uses dataclasses.replace — the new
    fields must survive that copy (a dropped field would silently un-pilot
    Cedric in every behavior test)."""
    a = _cedric_like()
    b = dataclasses.replace(a, require_wake_word=None)
    assert b.conversation_runtime == "elevenlabs_agent"
    assert b.elevenlabs_agent_id == "agent_test_123"
