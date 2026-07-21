"""Server-side product analytics (PostHog) — counts events, never content.

ONE place for the whole funnel: the marketing site (lauravatar.com carries the
PostHog JS snippet via Framer Custom Code) and the product backend (this
module) send to the SAME PostHog project, so "site visit → Try click →
signed up → ran a meeting" reads as one funnel in one dashboard.

OFF by default: with POSTHOG_API_KEY empty every call is a no-op — the
key-free demo, CI, and tests never see a network call. Delivery is a
fire-and-forget daemon thread with a short timeout: analytics must never
block a login or a meeting finalize, and a PostHog outage is invisible to
users (events are simply lost — acceptable for product metrics, never retry).

PRIVACY (hard rule): transcripts are PII and never leave the store. Events
carry ids and counters only — org_id, avatar_id, durations, action counts,
an email's DOMAIN — never meeting content, never a raw email address.
"""
from __future__ import annotations

import threading

from ..core.config import settings


def enabled() -> bool:
    return bool(settings.posthog_api_key.strip())


def capture(event: str, distinct_id: str, properties: dict | None = None) -> None:
    """Send one event, fire-and-forget. Never raises, never blocks."""
    if not enabled():
        return
    event = str(event or "").strip()
    distinct_id = str(distinct_id or "").strip()
    if not event or not distinct_id:
        return
    payload = {
        "api_key": settings.posthog_api_key.strip(),
        "event": event,
        "distinct_id": distinct_id,
        "properties": dict(properties or {}),
    }
    try:
        threading.Thread(target=_post, args=(payload,), daemon=True).start()
    except Exception:  # noqa: BLE001 — thread-spawn failure is not a login failure
        pass


def _post(payload: dict) -> None:
    try:
        import httpx

        httpx.post(
            settings.posthog_host.rstrip("/") + "/capture/",
            json=payload,
            timeout=5.0,
        )
    except Exception:  # noqa: BLE001 — analytics are best-effort by design
        pass
