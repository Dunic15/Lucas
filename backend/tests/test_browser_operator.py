"""Browser operator B0 — key-free invariants.

Flag-off inertness (every route 404s), the deterministic fake provider, the
policy classifier (read-only / guarded / blocked + sanitization), and the
opaque presentation-token helpers. No database, no vendors.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import browser, store
from app.browser import policy, tokens
from app.browser.fake_provider import FakeProvider
from app.config import settings


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeProvider._reset()
    yield
    FakeProvider._reset()


def test_disabled_by_default_and_routes_404(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    client = TestClient(main_module.app)
    assert browser.enabled() is False
    assert client.post("/org/browser/sessions", json={}).status_code == 404
    assert client.get("/org/browser/sessions/x").status_code == 404
    assert client.post(
        "/org/browser/present/exchange", json={}).status_code == 404
    assert client.get("/dashboard/browser/sessions/x").status_code == 404


def test_flag_alone_is_not_enough_without_control_plane(monkeypatch):
    monkeypatch.setattr(settings, "browser_operator_enabled", True)
    monkeypatch.setattr(settings, "laura_database_url", "")
    assert browser.enabled() is False


# ── fake provider determinism ───────────────────────────────────────────────

def test_fake_provider_is_deterministic_and_networkless():
    p = FakeProvider()
    s = p.create(ttl_seconds=1800)
    obs = p.observe(s.provider_ref)
    assert obs.url == "https://demo.laura.test/home"
    assert obs.title == "Laura Demo — Home"
    # Navigation history + click-follows-href are deterministic.
    p.navigate(s.provider_ref, "https://demo.laura.test/pricing")
    obs2 = p.click(s.provider_ref, "contact")
    assert obs2.url == "https://demo.laura.test/contact"
    # Same script from a fresh session yields the same observation.
    s2 = p.create(ttl_seconds=1800)
    p.navigate(s2.provider_ref, "https://demo.laura.test/pricing")
    obs3 = p.click(s2.provider_ref, "contact")
    assert obs3.url == obs2.url and obs3.title == obs2.title


def test_fake_provider_controlled_failures():
    from app.browser.provider import ProviderError, ProviderTimeout

    p = FakeProvider()
    s = p.create(ttl_seconds=1800)
    with pytest.raises(ProviderError):
        p.navigate(s.provider_ref, "fail://error")
    with pytest.raises(ProviderTimeout):
        p.navigate(s.provider_ref, "fail://timeout")


def test_fake_viewer_is_read_only_and_has_no_provider_url():
    p = FakeProvider()
    s = p.create(ttl_seconds=1800)
    viewer = p.viewer(s.provider_ref)
    assert viewer["read_only"] is True
    assert "connect_url" not in viewer and "provider_ref" not in viewer


# ── policy classifier ───────────────────────────────────────────────────────

def _obs_with(*elements):
    return {"url": "https://x", "elements": list(elements)}


def test_policy_classifies_read_guarded_blocked():
    # navigate / scroll / observe are auto.
    assert policy.classify("navigate", _obs_with())["class"] == "auto"
    assert policy.classify("scroll", _obs_with())["class"] == "auto"
    # click on a purchase/send/submit element is guarded.
    buy = {"id": "b", "role": "button", "name": "Buy", "kind": "purchase"}
    assert policy.classify("click", _obs_with(buy), element_id="b")["class"] \
        == "guarded"
    send = {"id": "s", "role": "button", "name": "Send", "kind": "send"}
    assert policy.classify("click", _obs_with(send), element_id="s")["class"] \
        == "guarded"
    # credential / secret / mfa fields are blocked outright.
    for kind in ("credential", "secret", "mfa", "download"):
        el = {"id": "x", "role": "textbox", "name": "f", "kind": kind}
        assert policy.classify("type", _obs_with(el), element_id="x",
                               text="v")["class"] == "blocked"
    # a name that looks like a credential is blocked even with a benign kind.
    pw = {"id": "p", "role": "textbox", "name": "Password", "kind": "text_input"}
    assert policy.classify("type", _obs_with(pw), element_id="p",
                           text="v")["class"] == "blocked"
    # coordinates-only (unresolvable element) click is refused.
    assert policy.classify("click", _obs_with(), element_id="ghost")["class"] \
        == "blocked"
    # a plain safe click is auto.
    link = {"id": "l", "role": "link", "name": "Home", "kind": "link"}
    assert policy.classify("click", _obs_with(link), element_id="l")["class"] \
        == "auto"


def test_policy_redacts_secrets():
    assert "[REDACTED]" in policy.redact("token sk-abcdefgh12345678 here")
    assert "[REDACTED]" in policy.redact("Authorization: Bearer abcdef123456789")
    assert policy.redact("nothing secret") == "nothing secret"


def test_sanitize_observation_marks_sensitive_and_redacts():
    class _Raw:
        url = "https://x"
        title = "T"
        dom_summary = "leaked sk-abcdefgh12345678 value"
        elements = [{"id": "p", "role": "textbox", "name": "Password",
                     "kind": "credential"}]
        screenshot_ref = "shot"
        truncated = False

    clean = policy.sanitize_observation(_Raw())
    assert "[REDACTED]" in clean["dom_summary"]
    assert clean["elements"][0]["sensitive"] is True


# ── token helpers ───────────────────────────────────────────────────────────

def test_token_mint_is_opaque_and_hash_stable():
    value, h = tokens.mint()
    assert tokens.looks_like_token(value)
    assert h == tokens.hash_token(value)
    assert h != value  # only the hash is ever stored
    v2, h2 = tokens.mint()
    assert v2 != value and h2 != h  # unpredictable
