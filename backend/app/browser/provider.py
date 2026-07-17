"""The provider-independent BrowserProvider boundary (B0).

ONE interface Laura owns; provider calls never spread into routes, meeting
code, or the frontend. A provider returns provider-native identifiers
(``provider_ref``) and a viewer payload — both stay behind the operator and
never become Laura's public API.

A provider deals in raw pages; the operator (operator.py) owns the state
machine, tenancy, policy classification and sanitization. Providers here do
NOT enforce policy — that is deterministic code between planner and provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


class ProviderUnconfigured(RuntimeError):
    """The provider needs credentials/config that are absent (flag-gated)."""


class ProviderError(RuntimeError):
    """A provider operation failed (mapped to session 'failed' upstream)."""


class ProviderTimeout(RuntimeError):
    """A provider operation timed out — the write MAY have landed."""


@dataclass
class ProviderSession:
    """What a provider hands back on create — the operator persists the ref
    and never exposes it."""
    provider_ref: str
    viewer_ref: str = ""  # provider-side viewer handle (never public/logged)


@dataclass
class RawObservation:
    """A provider's raw page read. The operator sanitizes this into the
    bounded, secret-free observation contract before it leaves the boundary."""
    url: str
    title: str
    dom_summary: str
    elements: list[dict] = field(default_factory=list)
    screenshot_ref: str = ""
    truncated: bool = False


class BrowserProvider(Protocol):
    name: str

    def create(self, *, ttl_seconds: int, profile: str = "") -> ProviderSession:
        ...

    def observe(self, provider_ref: str) -> RawObservation:
        ...

    def navigate(self, provider_ref: str, url: str) -> RawObservation:
        ...

    def click(self, provider_ref: str, element_id: str) -> RawObservation:
        ...

    def type_text(self, provider_ref: str, element_id: str,
                  text: str) -> RawObservation:
        ...

    def scroll(self, provider_ref: str, direction: str,
               amount: int = 1) -> RawObservation:
        ...

    def viewer(self, provider_ref: str) -> dict[str, Any]:
        """Fresh read-only viewer payload for a presentation-token exchange.
        Returns a dict the frontend can render; must NOT contain a permanent
        provider URL or credential that survives the session."""
        ...

    def close(self, provider_ref: str) -> None:
        ...


def get_provider(name: str) -> BrowserProvider:
    """Resolve the configured provider. The fake is always available; the
    Browserbase adapter is flag-gated and raises ProviderUnconfigured without
    keys (never fabricates a session)."""
    key = (name or "fake").strip().lower()
    if key in ("fake", ""):
        from .fake_provider import FakeProvider

        return FakeProvider()
    if key == "browserbase":
        from .browserbase_provider import BrowserbaseProvider

        return BrowserbaseProvider()
    raise ProviderUnconfigured(f"unknown browser provider {name!r}")


def default_provider_name() -> str:
    """The provider a new session uses: the configured one only when the real
    adapter is explicitly enabled AND configured; otherwise the fake. B0's
    tests and demos never need a real provider."""
    from ..config import settings

    if settings.browser_real_provider_enabled:
        return settings.browser_provider or "browserbase"
    return "fake"


_ = Optional  # re-exported type used by adapters
