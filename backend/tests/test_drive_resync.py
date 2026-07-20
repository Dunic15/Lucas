"""Continuous Drive sync: a connected `drive` source self-schedules its next
refresh after each SUCCESSFUL sync, so the folder stays current instead of a
one-time snapshot. Off when the interval is 0; a failed sync never reschedules
(its own retry handles transient errors, a dead source stops cleanly).

pg-free: the DAL and sync_drive are stubbed so we assert only the worker's
reschedule DECISION, not the SQL (that runs in the pg-marked suite)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.knowledge import ingest


def _job(kind: str = "sync_drive", sid: str = "src1") -> dict:
    return {"id": "job1", "lease_token": "lt", "attempts": 0, "kind": kind,
            "source_id": sid, "document_id": None}


def _wire(monkeypatch, *, job: dict, sync_ok: bool = True) -> list:
    """Stub the worker's DAL + sync so process_due runs one job, capturing any
    enqueue_job calls. Returns the capture list of (args, kwargs)."""
    served = {"done": False}

    def claim(org, n):
        if served["done"]:
            return []
        served["done"] = True
        return [job]

    def sync_drive(org, sid):
        if not sync_ok:
            raise RuntimeError("drive scope missing")
        return 3

    enq: list = []
    monkeypatch.setattr(ingest.dal, "due_orgs", lambda max_orgs=5: ["org1"])
    monkeypatch.setattr(ingest.dal, "claim_due_jobs", claim)
    monkeypatch.setattr(ingest.dal, "finish_job", lambda *a, **k: None)
    monkeypatch.setattr(ingest.dal, "mark_document_failed", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "sync_drive", sync_drive)
    monkeypatch.setattr(ingest.dal, "enqueue_job",
                        lambda *a, **k: enq.append((a, k)))
    return enq


def _resyncs(enq: list) -> list:
    return [e for e in enq if e[1].get("delay_seconds")]


def test_successful_sync_reschedules_deferred(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_drive_resync_seconds", 900.0)
    enq = _wire(monkeypatch, job=_job())
    ingest.process_due()
    resync = _resyncs(enq)
    assert len(resync) == 1, "expected exactly one deferred sync_drive re-enqueue"
    args, kwargs = resync[0]
    assert args[2] == "sync_drive"
    assert kwargs["delay_seconds"] == 900.0


def test_disabled_when_interval_zero(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_drive_resync_seconds", 0.0)
    enq = _wire(monkeypatch, job=_job())
    ingest.process_due()
    assert _resyncs(enq) == []  # single sync only


def test_failed_sync_does_not_reschedule(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_drive_resync_seconds", 900.0)
    enq = _wire(monkeypatch, job=_job(), sync_ok=False)
    ingest.process_due()
    assert _resyncs(enq) == []  # a failed sync must not perpetuate


def test_non_drive_job_never_reschedules(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_drive_resync_seconds", 900.0)
    # a rebuild_index job (not a drive sync) must not schedule a drive re-sync
    enq = _wire(monkeypatch, job=_job(kind="rebuild_index"))
    monkeypatch.setattr(ingest, "sync_local_indexes", lambda *a, **k: True)
    ingest.process_due()
    assert _resyncs(enq) == []
