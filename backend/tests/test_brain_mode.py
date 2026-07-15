"""Per-avatar brain toggle (dashboard): Gemini vs Cerebras, no redeploy.

Key-free. Covers: the persisted store, mode_for_avatar mapping + global
fallback, recall_client attaching the ears audio endpoint only for a
Gemini avatar, and the set-brain endpoint's validation.
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
    assert store.set_avatar_brain_mode("laura", "gemini")
    assert store.get_avatar_brain_mode("laura") == "gemini"
    assert store.set_avatar_brain_mode("laura", "cerebras")  # upsert
    assert store.get_avatar_brain_mode("laura") == "cerebras"
    assert store.all_avatar_brain_modes() == {"laura": "cerebras"}


def test_set_brain_mode_rejects_junk():
    assert not store.set_avatar_brain_mode("laura", "gpt5")
    assert not store.set_avatar_brain_mode("", "gemini")
    assert store.get_avatar_brain_mode("laura") is None


# ── mode resolution ────────────────────────────────────────────────────

def test_mode_for_avatar_maps_choice_over_global(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "off")  # global default
    store.set_avatar_brain_mode("laura", "gemini")
    store.set_avatar_brain_mode("cedric", "cerebras")
    assert gemini_ears.mode_for_avatar("laura") == "reply"     # gemini -> reply
    assert gemini_ears.mode_for_avatar("cedric") == "off"      # cerebras -> off
    assert gemini_ears.mode_for_avatar("sff") == "off"         # unset -> global


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


def test_gemini_avatar_gets_audio_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "off")
    store.set_avatar_brain_mode("laura", "gemini")
    attempts = _bodies("laura", monkeypatch)
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
    r = client.post("/avatars/laura/brain-mode", json={"brain": "gemini"})
    assert r.status_code == 200 and r.json()["brain"] == "gemini"
    assert store.get_avatar_brain_mode("laura") == "gemini"
    # junk value -> 400
    r = client.post("/avatars/laura/brain-mode", json={"brain": "gpt5"})
    assert r.status_code == 400
    # unknown avatar -> 404
    r = client.post("/avatars/nope/brain-mode", json={"brain": "gemini"})
    assert r.status_code == 404


def test_avatars_list_exposes_brain(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "off")
    store.set_avatar_brain_mode("laura", "gemini")
    client = TestClient(main_module.app)
    body = client.get("/avatars").json()
    laura = next(a for a in body["avatars"] if a["id"] == "laura")
    assert laura["brain"] == "gemini" and laura["brain_explicit"] is True
    cedric = next(a for a in body["avatars"] if a["id"] == "cedric")
    assert cedric["brain"] == "cerebras" and cedric["brain_explicit"] is False
