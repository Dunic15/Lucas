"""Production-hardening tests: per-IP rate limiting on the public, expensive,
UNAUTHENTICATED demo endpoints + security response headers — proving neither
touches the live-meeting path nor breaks Recall's iframe embedding of the avatar
page. No vendors, no keys, no network (the brain is monkeypatched).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main, security  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    """A TestClient with a FRESH limiter (the limiter is a process global that
    would otherwise leak counts between tests). Constructed without the lifespan
    context on purpose — no background loops / index prebuild needed here."""
    security.limiter.reset()
    yield TestClient(main.app)
    security.limiter.reset()


def _stub_answer(monkeypatch):
    """Neuter /demo/ask's brain so the test exercises the LIMITER, not RAG/LLM."""
    monkeypatch.setattr(
        main, "answer_question", lambda avatar, q: {"answer": "ok", "citations": []}
    )


# ───────────────────────── rate limiting ──────────────────────────
def test_demo_ask_429_after_limit_then_resets(client, monkeypatch):
    _stub_answer(monkeypatch)
    monkeypatch.setattr(settings, "rate_limit_demo_ask", 3)

    body = {"question": "hi", "avatar_id": "laura"}
    # First N within the window succeed.
    for _ in range(3):
        assert client.post("/demo/ask", json=body).status_code == 200

    # N+1 is throttled: 429 + Retry-After + a clear JSON message.
    blocked = client.post("/demo/ask", json=body)
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.json()["error"] == "rate_limited"

    # Resetting the window (here: clearing the in-process counters) lets it through
    # again — the limiter is a moving window, not a permanent ban.
    security.limiter.reset()
    assert client.post("/demo/ask", json=body).status_code == 200


def test_live_ask_streams_through_middleware_and_is_limited(client, monkeypatch):
    """The SSE streaming endpoints pass through the security-headers middleware:
    confirm the stream still arrives intact (headers layer doesn't buffer/break
    it) AND the limiter still guards it."""
    monkeypatch.setattr(
        main,
        "answer_question_stream",
        lambda avatar, q, **kwargs: iter(["Hello there.", "Second."]),
    )
    monkeypatch.setattr(settings, "rate_limit_live_ask", 2)
    body = {"question": "hi", "avatar_id": "laura"}

    r = client.post("/live/ask", json=body)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert r.headers.get("x-content-type-options") == "nosniff"  # headers survived
    assert 'data: {"content": "Hello there."}' in r.text
    assert "data: [DONE]" in r.text

    # Second call still ok, third throttled.
    assert client.post("/live/ask", json=body).status_code == 200
    assert client.post("/live/ask", json=body).status_code == 429


def test_post_meeting_is_the_strictest_bucket():
    """Sonnet-5 endpoint must default to the tightest limit of the five."""
    limits = {r: getattr(settings, knob) for r, knob in security.RATE_LIMITED_ROUTES.items()}
    assert limits["/demo/post_meeting"] == min(limits.values())


def test_x_forwarded_for_first_hop_buckets_per_client(client, monkeypatch):
    """App Runner is behind a proxy: the limiter must key on XFF's FIRST hop
    (the real client), so distinct clients get independent budgets and one
    abuser can't exhaust everyone's."""
    _stub_answer(monkeypatch)
    monkeypatch.setattr(settings, "rate_limit_demo_ask", 1)
    body = {"question": "hi", "avatar_id": "laura"}

    # Client A (first hop 9.9.9.9, proxy chain appended) burns its single token.
    hdr_a = {"X-Forwarded-For": "9.9.9.9, 10.0.0.1, 172.16.0.1"}
    assert client.post("/demo/ask", json=body, headers=hdr_a).status_code == 200
    assert client.post("/demo/ask", json=body, headers=hdr_a).status_code == 429

    # A DIFFERENT client (8.8.8.8) is unaffected — independent bucket.
    hdr_b = {"X-Forwarded-For": "8.8.8.8"}
    assert client.post("/demo/ask", json=body, headers=hdr_b).status_code == 200


def test_disable_flag_removes_all_limits(client, monkeypatch):
    """The master switch must make the limiter a no-op so it can never wedge a
    demo, even with an absurdly low configured limit."""
    _stub_answer(monkeypatch)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_demo_ask", 1)
    body = {"question": "hi", "avatar_id": "laura"}
    for _ in range(6):
        assert client.post("/demo/ask", json=body).status_code == 200


# ─────────── the LIVE meeting path is NEVER rate-limited ───────────
def test_live_meeting_paths_are_not_in_the_limiter_map():
    """Contract guard: the sacred live paths must not appear in the limiter's
    route map, so they can never be throttled regardless of limits."""
    for path in ("/webhooks/recall", "/webhooks/recall-calendar"):
        assert path not in security.RATE_LIMITED_ROUTES
    # And nothing session/avatar/ws-shaped leaked in.
    assert not any(
        p.startswith(("/sessions", "/avatar", "/ws")) for p in security.RATE_LIMITED_ROUTES
    )


def test_recall_webhook_never_throttled(client, monkeypatch):
    """Even with a limit of 1 and the limiter ENABLED, hammering the live
    transcript webhook never returns 429 — latency is the product there."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_demo_ask", 1)
    for _ in range(8):
        r = client.post("/webhooks/recall", json={"event": "ping"})
        assert r.status_code != 429
        assert r.status_code == 200


# ───────────────────────── security headers ───────────────────────
_EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-permitted-cross-domain-policies": "none",
}


def test_security_headers_present_on_normal_response(client):
    r = client.get("/health")
    assert r.status_code == 200
    for name, value in _EXPECTED_HEADERS.items():
        assert r.headers.get(name) == value
    hsts = r.headers.get("strict-transport-security")
    assert hsts and "max-age=" in hsts and "includeSubDomains" in hsts


def test_security_headers_also_on_the_429(client, monkeypatch):
    """The security layer wraps the rate-limit layer, so throttled responses
    still carry the headers (the outermost middleware stamps everything)."""
    _stub_answer(monkeypatch)
    monkeypatch.setattr(settings, "rate_limit_demo_ask", 1)
    body = {"question": "hi", "avatar_id": "laura"}
    assert client.post("/demo/ask", json=body).status_code == 200
    blocked = client.post("/demo/ask", json=body)
    assert blocked.status_code == 429
    assert blocked.headers.get("x-content-type-options") == "nosniff"


def test_hsts_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "hsts_max_age_seconds", 0)
    r = client.get("/health")
    assert "strict-transport-security" not in {k.lower() for k in r.headers}


# ── avatar / embed routes MUST stay iframe-embeddable (Recall camera) ──
@pytest.mark.parametrize("route", ["/talk", "/avatar", "/photoreal", "/live", "/join"])
def test_avatar_routes_not_frame_blocked(client, route):
    """Recall renders these in an IFRAME as the bot's camera. They must carry NO
    frame-blocking header, or the live avatar goes black. The other security
    headers should still be present (the middleware ran; it just didn't block)."""
    r = client.get(route)
    assert r.status_code == 200
    headers_lower = {k.lower(): v for k, v in r.headers.items()}
    assert "x-frame-options" not in headers_lower
    csp = headers_lower.get("content-security-policy", "")
    assert "frame-ancestors" not in csp
    # Middleware still applied the safe headers to the embeddable page.
    assert headers_lower.get("x-content-type-options") == "nosniff"


def test_no_frame_blocking_anywhere_by_default(client):
    """Belt-and-suspenders: we omit frame-blocking globally, so even the API/
    dashboard-shaped routes carry no X-Frame-Options (the deliberate, zero-risk
    choice — a broken avatar is far worse than a missing X-Frame-Options)."""
    for route in ("/health", "/avatars", "/"):
        r = client.get(route)
        assert "x-frame-options" not in {k.lower() for k in r.headers}
