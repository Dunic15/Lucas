"""Recall capacity errors are product-safe and actionable. No vendor calls."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import recall_client


def _body_variant(body):
    """Browser flavour a create_bot body asks for, read straight off the wire.

    Deliberately does NOT call recall_client's own helper: these tests must be
    able to run against the pre-fix client and fail on BEHAVIOUR (the fallback
    flavour is never reached) rather than on a missing attribute.
    """
    variant = (body or {}).get("variant")
    return variant.get("zoom") if isinstance(variant, dict) else None


def _record_variants(monkeypatch, responder):
    """Patch _request and return the list of browser flavours actually asked for."""
    monkeypatch.setattr(recall_client, "_headers", lambda: {"Authorization": "test"})
    seen: list[str | None] = []

    def fake_request(method, url, **kwargs):
        variant = _body_variant(kwargs.get("json"))
        seen.append(variant)
        return responder(httpx.Request(method, url), variant, len(seen))

    monkeypatch.setattr(recall_client, "_request", fake_request)
    return seen


def test_create_bot_maps_recall_507_to_avatar_busy(monkeypatch):
    # retry seam: exhaust the busy-retry budget instantly (no real waits)
    _sleeps = []
    monkeypatch.setattr(recall_client, "_BUSY_SLEEP", _sleeps.append)
    _record_variants(
        monkeypatch,
        lambda request, variant, n: httpx.Response(
            507, json={"detail": "vendor-internal"}, request=request
        ),
    )

    try:
        recall_client.create_bot(
            "https://meet.google.com/abc-defg-hij", "https://example.test/avatar"
        )
    except recall_client.AvatarBusyError as exc:
        assert str(exc) == recall_client.AVATAR_BUSY_MESSAGE
        assert "vendor-internal" not in str(exc)
        # the message must not promise a duration we cannot honour (2026-07-28:
        # the pool stayed dry for 17+ minutes while the copy said "a minute")
        assert "retry in a minute" not in str(exc)
    else:
        raise AssertionError("Recall 507 was not mapped to AvatarBusyError")
    # the transient-507 retry ran its full budget before giving up
    assert len(_sleeps) == recall_client._BUSY_RETRIES


def test_507_on_one_flavour_falls_through_to_the_next(monkeypatch):
    """REGRESSION (2026-07-28): a 507 used to `break` and replay the ladder from
    rung 0, so every retry re-asked web_gpu and web_4_core was NEVER reached —
    a busy GPU pool was unrecoverable even with other flavours free. The 507
    must now fall through to the next distinct flavour, with no backoff sleep."""
    waits = []
    monkeypatch.setattr(recall_client, "_BUSY_SLEEP", waits.append)

    def responder(request, variant, n):
        if variant == "web_gpu":  # GPU pool dry, everything else has slots
            return httpx.Response(507, json={"detail": "busy"}, request=request)
        return httpx.Response(201, json={"id": "bot-777"}, request=request)

    seen = _record_variants(monkeypatch, responder)
    result = recall_client.create_bot(
        "https://meet.google.com/abc-defg-hij", "https://example.test/avatar"
    )

    assert result["id"] == "bot-777"
    assert seen[0] == "web_gpu"  # preferred flavour is still tried first
    assert "web_4_core" in seen  # ...and the fallback is actually reached
    assert waits == []  # recovered within the round — no user-visible stall


def test_busy_flavour_is_not_replayed_within_a_round(monkeypatch):
    """Rungs that differ only in transcription/audio config share a browser
    flavour and would 507 identically. Asking a pool we just watched run dry
    burns the budget, so each distinct flavour is tried at most once a round."""
    monkeypatch.setattr(recall_client, "_BUSY_SLEEP", lambda _s: None)
    seen = _record_variants(
        monkeypatch,
        lambda request, variant, n: httpx.Response(
            507, json={"detail": "busy"}, request=request
        ),
    )

    try:
        recall_client.create_bot(
            "https://meet.google.com/abc-defg-hij", "https://example.test/avatar"
        )
    except recall_client.AvatarBusyError:
        pass

    rounds = recall_client._BUSY_RETRIES + 1
    distinct = len(set(seen))
    assert seen, "no dispatch was attempted"
    # every flavour the ladder offers is actually exercised — the pre-fix client
    # replayed web_gpu forever and never asked a second pool
    assert distinct >= 2, f"only tried {set(seen)}; fallback flavours unreached"
    # ...and none of them is asked twice in the same round
    assert len(seen) == distinct * rounds


def test_create_bot_busy_retry_recovers_when_capacity_frees(monkeypatch):
    """2026-07-24: Recall's shared avatar pool 507'd twice mid-demo with zero
    of our bots active, then cleared within minutes. When EVERY flavour is dry
    the round must back off and retry — never bounce the demo."""
    waits = []
    monkeypatch.setattr(recall_client, "_BUSY_SLEEP", waits.append)
    state = {"free": False}

    def responder(request, variant, n):
        if state["free"]:
            return httpx.Response(201, json={"id": "bot-777"}, request=request)
        return httpx.Response(507, json={"detail": "busy"}, request=request)

    seen = _record_variants(monkeypatch, responder)
    # capacity frees only after the first full round has been exhausted
    original_sleep = waits.append

    def free_after_first_backoff(seconds):
        original_sleep(seconds)
        state["free"] = True

    monkeypatch.setattr(recall_client, "_BUSY_SLEEP", free_after_first_backoff)

    result = recall_client.create_bot(
        "https://meet.google.com/abc-defg-hij", "https://example.test/avatar"
    )
    assert result["id"] == "bot-777"
    assert waits == [recall_client._BUSY_BACKOFF_S[0]]  # exactly one backoff
    assert seen[0] == "web_gpu"


def test_sessions_start_returns_friendly_avatar_busy_payload(monkeypatch):
    monkeypatch.setattr(recall_client, "assert_ready", lambda: None)

    async def busy(*args, **kwargs):
        raise recall_client.AvatarBusyError("raw error must not escape")

    monkeypatch.setattr(main_module, "_start_avatar_session", busy)
    # Do not enter TestClient's lifespan context here: its shutdown deliberately
    # sets main._shutting_down for the process, which would pollute later
    # reconciler tests in a full-suite run.
    client = TestClient(main_module.app)
    response = client.post(
        "/sessions/start",
        json={"meeting_url": "https://meet.google.com/abc-defg-hij"},
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.json() == {
        "error": "avatar_busy",
        # sourced from recall_client so the API copy can never drift from the
        # message create_bot actually raises
        "detail": recall_client.AVATAR_BUSY_MESSAGE,
    }


def test_dashboard_prefers_friendly_avatar_busy_detail():
    dashboard = (Path(__file__).resolve().parents[2] / "frontend/dashboard.html").read_text()
    assert 'res.j.error==="avatar_busy" ? res.j.detail' in dashboard
