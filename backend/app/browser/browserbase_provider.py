"""Smallest real Browserbase adapter behind BrowserProvider (B0, flag-gated).

DELIBERATELY INERT WITHOUT KEYS. Every method raises ``ProviderUnconfigured``
until ``BROWSER_REAL_PROVIDER_ENABLED`` is on AND ``BROWSERBASE_API_KEY`` /
``BROWSERBASE_PROJECT_ID`` are configured — the full test suite needs no
provider credentials. Real Playwright driving of the CDP session and the
OpenAI computer-use planner are B1 work; this adapter proves only that a real
provider slots behind the SAME interface without changing the operator.

Security: the provider API key and any live-view URL come ONLY from
environment/secret config — never source control, DB, logs, fixtures, or API
responses. Provider-native ids stay internal. The operator (not this adapter)
owns tenancy and never derives it from a provider session id.

What remains to be measured (NO fabricated numbers here): real create/observe/
navigate latency, live-view embed behaviour at Recall's 720p ceiling, session
reliability, and per-30-minute-session cost. See the B0 evaluation report.
"""
from __future__ import annotations

from typing import Any

from ..config import settings
from .provider import (
    BrowserProvider,
    ProviderSession,
    ProviderUnconfigured,
    RawObservation,
)


def _require_config() -> None:
    if not settings.browser_real_provider_enabled:
        raise ProviderUnconfigured(
            "browser_real_provider_enabled is off (B0 uses the fake provider)"
        )
    if not settings.browserbase_api_key or not settings.browserbase_project_id:
        raise ProviderUnconfigured(
            "BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID are not configured"
        )


class BrowserbaseProvider(BrowserProvider):  # type: ignore[misc]
    name = "browserbase"

    def create(self, *, ttl_seconds: int, profile: str = "") -> ProviderSession:
        _require_config()
        # B1: POST Browserbase /v1/sessions, connect Playwright to connect_url,
        # return the provider session id as provider_ref. Not implemented in B0.
        raise ProviderUnconfigured(
            "browserbase create is B1 — not implemented in B0"
        )

    def observe(self, provider_ref: str) -> RawObservation:
        _require_config()
        raise ProviderUnconfigured("browserbase observe is B1")

    def navigate(self, provider_ref: str, url: str) -> RawObservation:
        _require_config()
        raise ProviderUnconfigured("browserbase navigate is B1")

    def click(self, provider_ref: str, element_id: str) -> RawObservation:
        _require_config()
        raise ProviderUnconfigured("browserbase click is B1")

    def type_text(self, provider_ref: str, element_id: str,
                  text: str) -> RawObservation:
        _require_config()
        raise ProviderUnconfigured("browserbase type is B1")

    def scroll(self, provider_ref: str, direction: str,
               amount: int = 1) -> RawObservation:
        _require_config()
        raise ProviderUnconfigured("browserbase scroll is B1")

    def viewer(self, provider_ref: str) -> dict[str, Any]:
        _require_config()
        # B1: mint a fresh short-lived Browserbase live-view URL server-side.
        # The URL is returned to the caller ONCE per exchange and never
        # logged/stored — same discipline as the fake payload.
        raise ProviderUnconfigured("browserbase viewer is B1")

    def close(self, provider_ref: str) -> None:
        _require_config()
        raise ProviderUnconfigured("browserbase close is B1")
