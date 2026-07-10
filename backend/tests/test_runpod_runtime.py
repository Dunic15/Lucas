"""Runpod meeting-bound GPU: gate per-avatar + resume/stop schedulato. No keys."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import runpod_runtime as rp  # noqa: E402
from app.config import settings  # noqa: E402


def _arm(monkeypatch, calls):
    monkeypatch.setattr(settings, "runpod_api_key", "rpa_test")
    monkeypatch.setattr(settings, "runpod_pod_id", "pod123")
    monkeypatch.setattr(rp, "_gql", lambda q: calls.append(q) or {"data": {}})


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "runpod_api_key", "")
    monkeypatch.setattr(settings, "runpod_pod_id", "")
    assert not rp.enabled()
    rp.on_session_started("photoreal")  # non deve esplodere né chiamare nulla


def test_resume_only_for_photoreal_avatars(monkeypatch):
    calls: list[str] = []
    _arm(monkeypatch, calls)
    rp.on_session_started("talk")       # Cedric 3D: MAI accendere la GPU
    time.sleep(0.05)
    assert calls == []
    rp.on_session_started("photoreal")  # Laura: resume
    time.sleep(0.2)
    assert any("podResume" in c for c in calls)


def test_stop_scheduled_and_cancelled_by_new_session(monkeypatch):
    calls: list[str] = []
    _arm(monkeypatch, calls)
    monkeypatch.setattr(settings, "runpod_idle_stop_minutes", 0.002 / 60)  # ~2ms
    rp.configure(lambda: 0)
    rp.on_session_ended(active_count=0)     # schedula lo stop
    time.sleep(0.15)
    assert any("podStop" in c for c in calls), "stop non partito"

    calls.clear()
    rp.on_session_ended(active_count=0)     # ri-schedula...
    rp.on_session_started("photoreal")      # ...ma arriva una riunione: annulla
    time.sleep(0.15)
    assert not any("podStop" in c for c in calls), "stop NON annullato"
    assert any("podResume" in c for c in calls)


def test_stop_rechecks_active_sessions(monkeypatch):
    calls: list[str] = []
    _arm(monkeypatch, calls)
    monkeypatch.setattr(settings, "runpod_idle_stop_minutes", 0.002 / 60)
    rp.configure(lambda: 1)                 # al momento dello stop c'è gente
    rp.on_session_ended(active_count=0)
    time.sleep(0.15)
    assert not any("podStop" in c for c in calls)
