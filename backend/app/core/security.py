"""Production hardening: per-IP rate limiting + security response headers.

Two concerns, both registered by ``install(app)`` from main.py:

1. **Rate limiting** — an in-process sliding-window limiter on the PUBLIC,
   EXPENSIVE, UNAUTHENTICATED endpoints only (``/demo/ask``,
   ``/demo/post_meeting``, ``/tts``, ``/live/ask``, ``/live/act``). Those call an
   LLM (``/demo/post_meeting`` calls Sonnet-5 per request) or ElevenLabs, so an
   anonymous script hammering them is a real cost/abuse liability. The limiter
   NEVER runs on the live-meeting path (``/webhooks/recall``, ``ws/<id>``,
   ``/avatar/*``, ``/sessions/*``) or the authenticated dashboard — those are not
   in the route map, and websocket connections never reach HTTP middleware at
   all. Latency is the product on the live path; the map is exact-match, so the
   webhook/ws/sessions routes fall straight through with a single dict lookup.
   State is in-process, which is correct here: prod runs ONE App Runner instance
   (MaxSize=1). The whole thing is disable-able via ``settings.rate_limit_enabled``
   so it can never wedge a demo.

2. **Security headers** — HSTS, ``X-Content-Type-Options: nosniff``,
   ``Referrer-Policy``, ``X-Permitted-Cross-Domain-Policies`` on every response.
   Deliberately NO ``X-Frame-Options`` / CSP ``frame-ancestors``: Recall renders
   the avatar page (``/talk``, ``/avatar``, ``/photoreal``, ``/live``, ``/join``)
   in an IFRAME as the bot's camera, and a global frame-block would break the
   live avatar — far worse than a missing X-Frame-Options. (X-Frame-Options only
   affects documents loaded as frames, not the pages' fetch/XHR subresources like
   ``/tts`` or the ``.glb`` model, so omitting it is the clean, zero-risk choice.)

Both middlewares are non-blocking, do zero I/O, and add only microseconds — safe
on the ``/webhooks/recall`` HTTP path (the live transcript ingress).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import settings

# ── which endpoints are rate-limited, and the settings knob for each limit ──
# Exact-path match, POST only. Anything not in this map (the live/webhook/ws
# path, the dashboard, health, static pages) is never limited.
RATE_LIMITED_ROUTES: dict[str, str] = {
    "/demo/ask": "rate_limit_demo_ask",
    "/demo/post_meeting": "rate_limit_demo_post_meeting",
    "/tts": "rate_limit_tts",
    "/live/ask": "rate_limit_live_ask",
    "/live/act": "rate_limit_live_act",
}


def client_ip(request: Request) -> str:
    """Best-effort real client IP.

    App Runner sits behind a proxy, so the immediate peer (``request.client``) is
    the proxy. ``X-Forwarded-For`` is ``client, proxy1, proxy2, …`` — the FIRST
    hop is the original client, which is what we key the limiter on. Falls back to
    the socket peer, then a constant so a missing address still buckets together.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


class SlidingWindowLimiter:
    """Per-key sliding-window counter. Thread-safe (routes run in a threadpool).

    Keeps, per ``(ip, route)`` key, a deque of hit timestamps within the window;
    a request is allowed iff fewer than ``limit`` hits remain after evicting the
    expired ones. Memory is bounded by opportunistic cleanup: an emptied key is
    dropped, and a periodic sweep clears keys whose windows have all expired, so a
    burst of unique IPs can't leak unboundedly.
    """

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def check(self, key: tuple[str, str], limit: int, window: float) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``. On allow, records the hit."""
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            dq = self._hits.get(key)
            if dq is None:
                dq = deque()
                self._hits[key] = dq
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                # Seconds until the oldest hit ages out of the window (ceil, ≥1).
                retry_after = max(1, int(dq[0] + window - now) + 1)
                return False, retry_after
            dq.append(now)
            if not dq:  # (can't happen after append, kept for symmetry)
                self._hits.pop(key, None)
            self._maybe_sweep(now, window)
            return True, 0

    def _maybe_sweep(self, now: float, window: float) -> None:
        """Drop keys whose every hit has expired. Cheap, at most every window."""
        if now - self._last_sweep < window:
            return
        self._last_sweep = now
        cutoff = now - window
        stale = [k for k, dq in self._hits.items() if not dq or dq[-1] <= cutoff]
        for k in stale:
            self._hits.pop(k, None)

    def reset(self) -> None:
        """Wipe all counters (tests + a clean slate on config flip)."""
        with self._lock:
            self._hits.clear()
            self._last_sweep = 0.0


# Module-global limiter: one instance for the process (single App Runner box).
limiter = SlidingWindowLimiter()


def _limit_for(knob: str) -> int:
    """Resolve the current per-window limit off ``settings`` for a route's config
    knob. Read live so tests/env can change it without a rebuild."""
    try:
        return int(getattr(settings, knob))
    except (TypeError, ValueError):
        return 0


# Standard security headers. HSTS is added separately (its max-age is a knob and
# 0 means "omit"). NOTE: intentionally NO X-Frame-Options / frame-ancestors —
# see the module docstring (Recall iframe-embeds the avatar page).
_STATIC_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
}


def install(app: FastAPI) -> None:
    """Register dashboard UI, rate-limit, and security-header middleware.

    Order matters: the security-headers middleware is added LAST so it is the
    OUTERMOST layer and stamps headers on every response — including the 429 the
    rate-limit layer short-circuits with.
    """
    # Presentation-only owner-dashboard preference. Imported lazily so security
    # remains independently testable and the live path pays no module-level work.
    from .. import dashboard_runtime_ui

    dashboard_runtime_ui.install(app)

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next: Callable):
        # Cheapest possible fall-through for the hot/live path: one flag + one
        # dict lookup, no IP parsing, no locking, before we defer to the app.
        if settings.rate_limit_enabled and request.method == "POST":
            knob = RATE_LIMITED_ROUTES.get(request.url.path)
            if knob is not None:
                limit = _limit_for(knob)
                if limit > 0:
                    ip = client_ip(request)
                    allowed, retry_after = limiter.check(
                        (ip, request.url.path),
                        limit,
                        settings.rate_limit_window_seconds,
                    )
                    if not allowed:
                        return JSONResponse(
                            {
                                "error": "rate_limited",
                                "detail": (
                                    "Too many requests — you're going faster than "
                                    "this demo endpoint allows. Wait a moment and "
                                    "try again."
                                ),
                            },
                            status_code=429,
                            headers={"Retry-After": str(retry_after)},
                        )
        return await call_next(request)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Callable):
        response = await call_next(request)
        if settings.security_headers_enabled:
            for name, value in _STATIC_SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
            if settings.hsts_max_age_seconds > 0:
                response.headers.setdefault(
                    "Strict-Transport-Security",
                    f"max-age={int(settings.hsts_max_age_seconds)}; includeSubDomains",
                )
        return response
