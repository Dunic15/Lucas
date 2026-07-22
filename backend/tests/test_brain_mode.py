"""Per-avatar brain setting (dashboard) — Cerebras only since 2026-07-22.

Gemini was RETIRED from the selectable set after the 2026-07-21 live hijack
(a store row lost across deploys dropped an avatar onto the relay brain,
which bypasses persona/registry/playbooks). The relay is now reachable only
via the global GEMINI_EARS_MODE env. Covers: the store rejects/ignores
gemini (including LEGACY rows), mode_for_avatar mapping + global fallback,
recall_client ears attachment, and the set-brain endpoint's validation.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app import gemini_ears, recall_client, store
from app.config import settings


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()


# ── storage ────────────────────────────────────────────────────────────

def test_set_get_brain_mode_roundtrip():
    assert store.get_avatar_brain_mode("laura") is None
    assert store.set_avatar_brain_mode("laura", "cerebras")
    assert store.get_avatar_brain_mode("laura") == "cerebras"
    assert store.set_avatar_brain_mode("laura", "cerebras")  # upsert
    assert store.get_avatar_brain_mode("laura") == "cerebras"
    assert store.all_avatar_brain_modes() == {"laura": "cerebras"}


def _seed_legacy_gemini_row(avatar_id: str) -> None:
    """Simulate a pre-retirement row (set_avatar_brain_mode refuses it now)."""
    import time as _t

    with store._LOCK, store._connect() as conn:
        conn.execute(
            "INSERT INTO avatar_brain_mode (avatar_id, brain_mode, updated_at) "
            "VALUES (?, 'gemini', ?) ON CONFLICT(avatar_id) DO UPDATE SET "
            "brain_mode = 'gemini'",
            (avatar_id, _t.time()),
        )


def test_legacy_gemini_row_reads_as_unset():
    """A stale 'gemini' row must never resurrect the relay brain — it reads
    as None so the caller falls to the global env default."""
    _seed_legacy_gemini_row("laura")
    assert store.get_avatar_brain_mode("laura") is None


def test_set_brain_mode_rejects_junk():
    assert not store.set_avatar_brain_mode("laura", "gpt5")
    assert not store.set_avatar_brain_mode("laura", "gemini")  # retired
    assert not store.set_avatar_brain_mode("", "cerebras")
    assert store.get_avatar_brain_mode("laura") is None


# ── mode resolution ────────────────────────────────────────────────────

def test_mode_for_avatar_maps_choice_over_global(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")  # global gemini
    _seed_legacy_gemini_row("laura")
    store.set_avatar_brain_mode("cedric", "cerebras")
    assert gemini_ears.mode_for_avatar("cedric") == "off"      # cerebras -> off
    assert gemini_ears.mode_for_avatar("sff") == "reply"       # unset -> global
    # A legacy gemini row is UNSET, not a grant: it follows the global too.
    assert gemini_ears.mode_for_avatar("laura") == "reply"
    monkeypatch.setattr(settings, "gemini_ears_mode", "off")
    assert gemini_ears.mode_for_avatar("laura") == "off"


def test_mode_for_avatar_unset_uses_global(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")
    assert gemini_ears.mode_for_avatar("sff") == "reply"       # unset -> global


# ── recall_client attaches the ears endpoint per avatar ────────────────

def _bodies(avatar_id, monkeypatch):
    monkeypatch.setattr(settings, "ears_relay_ws_base", "wss://relay.example")
    from unittest.mock import patch

    # create_bot needs to compute attach_ears from the avatar; exercise via the
    # attempts builder to check the audio endpoint is attached only when the
    # avatar's effective mode is enabled.
    from app import gemini_ears as ge

    attach = ge.mode_enabled(ge.mode_for_avatar(avatar_id)) and bool(
        settings.ears_relay_ws_base.strip()
    )
    return recall_client._create_bot_attempts(
        "https://meet.google.com/abc-defg-hij",
        "https://example.test/talk",
        join_at=None,
        realtime_capability="cap",
        attach_ears=attach,
    )


def test_legacy_gemini_avatar_gets_no_audio_endpoint(monkeypatch):
    """Pre-retirement behavior inverted: a stale 'gemini' row no longer
    attaches the ears endpoint when the global default is off."""
    monkeypatch.setattr(settings, "gemini_ears_mode", "off")
    _seed_legacy_gemini_row("laura")
    attempts = _bodies("laura", monkeypatch)
    assert all(not l.endswith("+gemini-ears") for l, _ in attempts)


def test_global_reply_still_attaches_ears_for_unset_avatar(monkeypatch):
    """The env escape hatch stays: global reply + no explicit choice = ears."""
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")
    attempts = _bodies("sff", monkeypatch)
    assert any(l.endswith("+gemini-ears") for l, _ in attempts)


def test_cerebras_avatar_has_no_audio_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")  # global is gemini
    store.set_avatar_brain_mode("cedric", "cerebras")           # but this one opts out
    attempts = _bodies("cedric", monkeypatch)
    assert all(not l.endswith("+gemini-ears") for l, _ in attempts)
    for _, body in attempts:
        assert "audio_mixed_raw" not in body["recording_config"]


# ── set-brain endpoint ─────────────────────────────────────────────────

def test_set_brain_endpoint_validates(monkeypatch):
    # key-free: no token + login disabled -> auth.gate is open (demo)
    client = TestClient(main_module.app)
    r = client.post("/avatars/laura/brain-mode", json={"brain": "cerebras"})
    assert r.status_code == 200 and r.json()["brain"] == "cerebras"
    assert store.get_avatar_brain_mode("laura") == "cerebras"
    # gemini is retired from the UI -> 400 (env-gated only)
    r = client.post("/avatars/laura/brain-mode", json={"brain": "gemini"})
    assert r.status_code == 400
    # junk value -> 400
    r = client.post("/avatars/laura/brain-mode", json={"brain": "gpt5"})
    assert r.status_code == 400
    # unknown avatar -> 404
    r = client.post("/avatars/nope/brain-mode", json={"brain": "cerebras"})
    assert r.status_code == 404


def test_avatars_list_exposes_brain(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "off")
    store.set_avatar_brain_mode("laura", "cerebras")
    _seed_legacy_gemini_row("cedric")  # stale row must read as unset
    client = TestClient(main_module.app)
    body = client.get("/avatars").json()
    laura = next(a for a in body["avatars"] if a["id"] == "laura")
    assert laura["brain"] == "cerebras" and laura["brain_explicit"] is True
    cedric = next(a for a in body["avatars"] if a["id"] == "cedric")
    assert cedric["brain"] == "cerebras" and cedric["brain_explicit"] is False
