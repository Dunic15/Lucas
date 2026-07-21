"""The /health/graphiti admin endpoint; status + optional live smoke test.

Lets an operator verify a deploy's knowledge-graph wiring (Neo4j creds +
Anthropic extraction + runtime Python version, the #319 incident's root cause)
end-to-end without a live meeting: GET ?run=1 runs a real ingest→recall against
a dedicated __smoke__ group. Here we cover the cheap paths (status shape,
disabled-guard, auth gate); the live round-trip needs a real graph DB, so it
is exercised on the deploy, not in the key-free suite.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main as main_module  # noqa: E402
from app.config import settings  # noqa: E402


def test_status_shape_when_off():
    """Off by default: status reports the flags, never crashes, no live block."""
    client = TestClient(main_module.app)
    r = client.get("/health/graphiti")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["configured"] is False
    assert "graphiti_core" in body and "embedding_dim" in body
    assert body["python"].count(".") == 2  # e.g. "3.12.4": the runtime readout
    assert "live" not in body  # no ?run → no round-trip


def test_run_when_disabled_returns_503_hint():
    """?run=1 while disabled short-circuits with a 503 + a config hint, rather
    than trying (and failing) to reach a graph DB."""
    client = TestClient(main_module.app)
    r = client.get("/health/graphiti", params={"run": 1})
    assert r.status_code == 503
    assert r.json()["live"]["stage"] == "disabled"


def test_auth_gated(monkeypatch):
    """Like /health/vendors: when LAURA_API_TOKEN is set, the endpoint requires
    the bearer (statuses reveal what's configured)."""
    monkeypatch.setattr(settings, "laura_api_token", "s3cret")
    client = TestClient(main_module.app)
    assert client.get("/health/graphiti").status_code == 401
    ok = client.get(
        "/health/graphiti", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
