"""Deterministic, network-free browser provider (B0).

Every observation is a pure function of (start pages, navigation/command
history) — reproducible for tests, demos, and the frontend fixtures. It
supports deterministic pages, navigation history, clicks + text entry,
CONTROLLED FAILURES (magic URLs), session expiration, and a fake read-only
viewer payload. No credentials, no sockets, ever.

Controlled-failure URLs (so tests exercise the operator's failure handling
without a real browser):
  fail://error     -> ProviderError on the operation
  fail://timeout   -> ProviderTimeout (the write MAY have landed)
  fail://expire    -> the provider marks its session gone (operator -> expired)
"""
from __future__ import annotations

import threading
from typing import Any

from .provider import (
    ProviderError,
    ProviderSession,
    ProviderTimeout,
    RawObservation,
)

# A small deterministic web: url -> {title, dom, elements}. Elements carry a
# stable id, role, name, and a 'kind' the policy engine classifies.
_PAGES: dict[str, dict[str, Any]] = {
    "https://demo.laura.test/home": {
        "title": "Laura Demo — Home",
        "dom": "Welcome to the Laura product demo. Explore features below.",
        "elements": [
            {"id": "nav-pricing", "role": "link", "name": "Pricing",
             "kind": "link", "href": "https://demo.laura.test/pricing"},
            {"id": "nav-signup", "role": "link", "name": "Create account",
             "kind": "account_creation",
             "href": "https://demo.laura.test/signup"},
            {"id": "search", "role": "textbox", "name": "Search",
             "kind": "text_input"},
        ],
    },
    "https://demo.laura.test/pricing": {
        "title": "Laura Demo — Pricing",
        "dom": "Team plan: 20 EUR per seat per month. Enterprise: contact us.",
        "elements": [
            {"id": "buy-team", "role": "button", "name": "Buy Team plan",
             "kind": "purchase"},
            {"id": "contact", "role": "link", "name": "Contact sales",
             "kind": "link", "href": "https://demo.laura.test/contact"},
        ],
    },
    "https://demo.laura.test/contact": {
        "title": "Laura Demo — Contact",
        "dom": "Reach the sales team.",
        "elements": [
            {"id": "msg", "role": "textbox", "name": "Message",
             "kind": "text_input"},
            {"id": "send", "role": "button", "name": "Send message",
             "kind": "send"},
            {"id": "secret-field", "role": "textbox", "name": "API token",
             "kind": "secret"},
        ],
    },
    "https://demo.laura.test/login": {
        "title": "Laura Demo — Sign in",
        "dom": "Sign in to your account.",
        "elements": [
            {"id": "user", "role": "textbox", "name": "Email",
             "kind": "text_input"},
            {"id": "pass", "role": "textbox", "name": "Password",
             "kind": "credential"},
            {"id": "otp", "role": "textbox", "name": "One-time code",
             "kind": "mfa"},
        ],
    },
}
_HOME = "https://demo.laura.test/home"
_MISSING_PAGE = {
    "title": "Not found", "dom": "This page does not exist.", "elements": [],
}


class _State:
    __slots__ = ("url", "history", "gone", "typed")

    def __init__(self):
        self.url = _HOME
        self.history: list[str] = [_HOME]
        self.gone = False
        self.typed: dict[str, str] = {}


class FakeProvider:
    name = "fake"
    _lock = threading.Lock()
    _sessions: dict[str, _State] = {}
    _counter = 0

    def _get(self, provider_ref: str) -> _State:
        with self._lock:
            state = self._sessions.get(provider_ref)
        if state is None or state.gone:
            raise ProviderError("session gone")
        return state

    def create(self, *, ttl_seconds: int, profile: str = "") -> ProviderSession:
        with FakeProvider._lock:
            FakeProvider._counter += 1
            ref = f"fake-prov-{FakeProvider._counter}"
            FakeProvider._sessions[ref] = _State()
        return ProviderSession(provider_ref=ref, viewer_ref=f"{ref}-viewer")

    def _observe_state(self, state: _State) -> RawObservation:
        page = _PAGES.get(state.url, _MISSING_PAGE)
        dom = page["dom"]
        # Reflect typed values so tests can assert entry, but NEVER echo a
        # credential/secret/mfa value into the DOM summary (the operator also
        # redacts; this is defense in depth at the source).
        return RawObservation(
            url=state.url,
            title=page["title"],
            dom_summary=dom[:2000],
            elements=[dict(e) for e in page["elements"]],
            screenshot_ref=f"fake-shot:{state.url}",
            truncated=len(dom) > 2000,
        )

    def _apply_failure(self, url: str, provider_ref: str) -> None:
        if url == "fail://error":
            raise ProviderError("controlled failure")
        if url == "fail://timeout":
            raise ProviderTimeout("controlled timeout")
        if url == "fail://expire":
            with self._lock:
                st = self._sessions.get(provider_ref)
                if st:
                    st.gone = True
            raise ProviderError("session expired at provider")

    def observe(self, provider_ref: str) -> RawObservation:
        return self._observe_state(self._get(provider_ref))

    def navigate(self, provider_ref: str, url: str) -> RawObservation:
        self._apply_failure(url, provider_ref)
        state = self._get(provider_ref)
        state.url = url
        state.history.append(url)
        return self._observe_state(state)

    def click(self, provider_ref: str, element_id: str) -> RawObservation:
        state = self._get(provider_ref)
        page = _PAGES.get(state.url, _MISSING_PAGE)
        for el in page["elements"]:
            if el["id"] == element_id and el.get("href"):
                state.url = el["href"]
                state.history.append(el["href"])
                break
        return self._observe_state(state)

    def type_text(self, provider_ref: str, element_id: str,
                  text: str) -> RawObservation:
        state = self._get(provider_ref)
        state.typed[element_id] = text
        return self._observe_state(state)

    def scroll(self, provider_ref: str, direction: str,
               amount: int = 1) -> RawObservation:
        return self._observe_state(self._get(provider_ref))

    def viewer(self, provider_ref: str) -> dict[str, Any]:
        state = self._get(provider_ref)
        page = _PAGES.get(state.url, _MISSING_PAGE)
        # A read-only fake viewer payload: a rendered snapshot, NO permanent
        # provider URL, NO credential — safe to hand a watcher after the
        # server-side token exchange. Dies with the session.
        return {
            "kind": "fake_readonly",
            "url": state.url,
            "title": page["title"],
            "rendered_text": page["dom"][:2000],
            "read_only": True,
        }

    def close(self, provider_ref: str) -> None:
        with self._lock:
            st = self._sessions.get(provider_ref)
            if st:
                st.gone = True

    # Test helpers (never called by production paths).
    @classmethod
    def _reset(cls) -> None:
        with cls._lock:
            cls._sessions.clear()
            cls._counter = 0
