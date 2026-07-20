"""Browser B1 "Visual Eyes" — key-free invariants.

The full screenshot→plan→validate→policy→execute→verify loop on the
deterministic fake provider + fake visual planner. No model key, no network.
Covers: byte-free observations, screenshot bounds, secret exclusion,
structured-proposal rejection, coordinate grounding, domain restriction,
prompt injection, the bounded coordinator + its limits, flag-off identity.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import browser, store
from app.browser import (contracts, coordinator, multimodal, operator, planner,
                         policy)
from app.browser.fake_provider import FakeProvider
from app.config import settings


@pytest.fixture(autouse=True)
def _reset():
    FakeProvider._reset()
    yield
    FakeProvider._reset()


# ── flag-off identity ───────────────────────────────────────────────────────

def test_visual_planner_off_by_default_and_coordinator_inert(monkeypatch):
    assert settings.browser_visual_planner_enabled is False
    assert coordinator.run("o", "s", "goal")["outcome"] == "disabled"
    # The multimodal adapter is unconfigured without the flag+key.
    assert multimodal.get_visual_planner() is None


def test_run_route_404_when_visual_planner_off(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    client = TestClient(main_module.app)
    # /run 404s (feature off) exactly like every browser route with flags off.
    assert client.post("/org/browser/sessions/x/run",
                       json={"goal": "g"}).status_code == 404


# ── observation contract: byte-free, bounded, secret-free ───────────────────

def test_observation_is_byte_free_and_bounded():
    p = FakeProvider()
    s = p.create(ttl_seconds=600)
    raw = p.navigate(s.provider_ref, "https://demo.laura.test/visual")
    assert raw.screenshot_bytes  # provider DID capture a screenshot
    sanitized = policy.sanitize_observation(raw)
    obs = contracts.build_observation(
        session_id="sess", command_sequence=1, page_version=1,
        sanitized=sanitized, timestamp=1.0, org_id="org", principal="u")
    blob = json.dumps(obs)
    # No raw bytes, no base64/data image anywhere in the observation.
    assert "screenshot_bytes" not in obs
    assert "data:image" not in blob
    # Only a ref + digest survive.
    assert obs["screenshot_digest"] and len(obs["screenshot_digest"]) == 64
    assert obs["screenshot_ref"].startswith("fake-shot:")
    assert obs["screenshot_bytes_len"] > 0
    # Distinct a11y summary + real viewport + element geometry present.
    assert obs["a11y_summary"] and obs["a11y_summary"] != obs["dom_summary"]
    assert obs["viewport"] == {"width": 1280, "height": 720}
    assert all("bbox" in e for e in obs["elements"])
    assert obs["org_id"] == "org" and obs["observation_id"] == "obs:sess:1"


def test_build_observation_rejects_inline_image():
    with pytest.raises(ValueError):
        contracts.build_observation(
            session_id="s", command_sequence=1, page_version=1,
            sanitized={"screenshot_ref": "data:image/png;base64,AAAA"},
            timestamp=1.0)


def test_secret_values_excluded_from_observation():
    # The trap page embeds an sk- secret in its text; sanitize redacts it.
    p = FakeProvider()
    s = p.create(ttl_seconds=600)
    raw = p.navigate(s.provider_ref, "https://demo.laura.test/trap")
    obs = policy.sanitize_observation(raw)
    assert "sk-livesecret" not in json.dumps(obs)
    assert "[REDACTED]" in obs["dom_summary"]


def test_screenshot_bound_fails_closed_when_too_big(monkeypatch):
    monkeypatch.setattr(settings, "browser_screenshot_max_bytes", 10)
    # Over the cap and un-shrinkable (no Pillow / not an image) → raise.
    with pytest.raises(multimodal.PlannerError):
        multimodal.bound_screenshot(b"x" * 100)
    assert multimodal.bound_screenshot(b"tiny") == b"tiny"


# ── strict proposal validation (fail-closed on model output) ────────────────

def test_validate_proposal_rejects_malformed_and_unknown():
    vp = {"width": 1280, "height": 720}
    assert not contracts.validate_proposal("nope", viewport=vp)["ok"]
    assert not contracts.validate_proposal(
        {"operation": "click", "surprise": 1}, viewport=vp)["ok"]
    assert not contracts.validate_proposal(
        {"operation": "exfiltrate"}, viewport=vp)["ok"]
    # coordinate out of viewport rejected
    bad = {"operation": "click", "target_type": "coordinates",
           "coordinates": [5000, 5000]}
    assert contracts.validate_proposal(
        bad, viewport=vp)["reason"] == "coordinate_out_of_viewport"
    # a clean coordinate proposal validates
    good = {"operation": "click", "target_type": "coordinates",
            "coordinates": [100, 100], "confidence": 0.9}
    assert contracts.validate_proposal(good, viewport=vp)["ok"]


# ── deterministic policy additions ──────────────────────────────────────────

def test_navigation_allowlist_is_server_authoritative():
    allowed = policy.allowed_domain_set("demo.laura.test")
    assert policy.check_navigation_target(
        "https://demo.laura.test/pricing", allowed)["ok"]
    assert policy.check_navigation_target(
        "https://sub.demo.laura.test/x", allowed)["ok"]  # subdomain ok
    assert policy.check_navigation_target(
        "https://evil.example.com/steal", allowed)["reason"] == "domain_blocked"
    assert policy.check_navigation_target(
        "javascript:alert(1)", allowed)["reason"] == "scheme_not_allowed"
    assert policy.check_navigation_target(
        "file:///etc/passwd", allowed)["reason"] == "scheme_not_allowed"


def test_coordinate_hit_test_resolves_to_element():
    obs = {"elements": [
        {"id": "a", "bbox": [0, 0, 100, 50]},
        {"id": "b", "bbox": [200, 0, 300, 50]}]}
    assert policy.resolve_coordinate(obs, 50, 25) == "a"
    assert policy.resolve_coordinate(obs, 250, 25) == "b"
    assert policy.resolve_coordinate(obs, 150, 25) is None  # the gap


# ── adversarial-finding regressions ─────────────────────────────────────────

def test_sensitive_element_value_never_in_name():
    """Adversarial: a credential/mfa element's value/name must never survive
    into the observation, even if a provider sourced it (defense in depth)."""
    from app.browser.provider import RawObservation

    raw = RawObservation(
        url="https://x", title="t", dom_summary="d",
        elements=[{"id": "p", "role": "textbox", "name": "hunter2",
                   "kind": "credential"},
                  {"id": "o", "role": "textbox", "name": "483920",
                   "kind": "mfa"}])
    clean = policy.sanitize_observation(raw)
    assert clean["elements"][0]["name"] == ""  # blanked, not passed through
    assert clean["elements"][1]["name"] == ""
    assert "hunter2" not in json.dumps(clean)
    assert "483920" not in json.dumps(clean)


def test_screenshot_ref_redacts_embedded_secret():
    """Adversarial: a fake screenshot_ref embeds the URL — a secret-shaped
    token in it must be redacted, matching url redaction."""
    from app.browser.provider import RawObservation

    raw = RawObservation(
        url="https://demo.laura.test/r?token=ghp_" + "a" * 30,
        title="t", dom_summary="d",
        screenshot_ref="fake-shot:https://demo.laura.test/r?token=ghp_"
        + "a" * 30)
    clean = policy.sanitize_observation(raw)
    assert "ghp_" not in clean["screenshot_ref"]
    assert "[REDACTED]" in clean["screenshot_ref"]


def test_browserbase_selector_is_synthetic_and_injection_safe():
    """Adversarial: the real provider addresses elements by a server-minted
    synthetic handle, never a page id interpolated into a CSS selector."""
    from app.browser import browserbase_provider as bb

    # A page-controlled id with CSS metacharacters cannot expand a selector
    # list — it is a quoted attribute-value match on the synthetic handle.
    sel = bb._el_selector("x,.buy-now")
    assert sel == '[data-laura-el="x,.buy-now"]'
    assert sel.startswith("[data-laura-el=") and "#" not in sel
    # The observe JS mints synthetic ids and never READS el.value (the string
    # only appears in a "NEVER read el.value" comment, not as a value read).
    assert "data-laura-el" in bb._OBSERVE_JS
    assert "|| el.value" not in bb._OBSERVE_JS
    assert "el.value ||" not in bb._OBSERVE_JS


# ── FakeVisualPlanner scenarios ─────────────────────────────────────────────

def _obs_for(url):
    p = FakeProvider()
    s = p.create(ttl_seconds=600)
    raw = p.navigate(s.provider_ref, url)
    obs = policy.sanitize_observation(raw)
    obs["page_version"] = 1
    obs["observation_id"] = "obs:x:1"
    return contracts.build_observation(
        session_id="x", command_sequence=1, page_version=1, sanitized=obs,
        timestamp=1.0), raw.screenshot_bytes


def test_fake_visual_planner_grounds_target_from_screenshot():
    obs, shot = _obs_for("https://demo.laura.test/visual")
    pl = planner.FakeVisualPlanner(mode="visual_select")
    prop = pl.propose(obs, "continue", screenshot=shot)
    # It picks the screenshot-only primary (right) by COORDINATES — the DOM
    # text ("Continue"/"Continue") cannot disambiguate.
    assert prop["operation"] == "click"
    assert prop["target_type"] == "coordinates"
    # the coordinate is the centre of continue-right's bbox [1020,480,1200,528]
    assert 1020 <= prop["coordinates"][0] <= 1200


@pytest.mark.parametrize("mode,page,expect", [
    ("malformed", "pricing", False), ("unknown_op", "pricing", False),
    ("wrong_target", "visual", True), ("low_confidence", "visual", True),
    ("guarded", "pricing", True), ("blocked", "login", True),
])
def test_fake_visual_planner_modes_validate_or_reject(mode, page, expect):
    obs, shot = _obs_for(f"https://demo.laura.test/{page}")
    prop = planner.FakeVisualPlanner(mode=mode).propose(obs, "g", screenshot=shot)
    verdict = contracts.validate_proposal(prop, viewport=obs["viewport"])
    assert verdict["ok"] is expect
    if mode == "low_confidence":
        assert verdict["proposal"]["confidence"] < 0.6


# ── anthropic planner branch (key-free: SDK mocked, no network) ─────────────

def _enable_anthropic_planner(monkeypatch, key="k"):
    monkeypatch.setattr(settings, "browser_visual_planner_enabled", True)
    monkeypatch.setattr(settings, "browser_planner_provider", "anthropic")
    monkeypatch.setattr(settings, "browser_planner_model", "")
    monkeypatch.setattr(settings, "anthropic_api_key", key)


def test_anthropic_provider_defaults_model_and_reuses_brain_key(monkeypatch):
    _enable_anthropic_planner(monkeypatch)
    p = multimodal.MultimodalPlanner()
    assert p._provider == "anthropic"
    assert p._model == "claude-opus-4-8"  # per-provider default
    assert p._api_key == "k"  # settings.anthropic_api_key, no new secret


def test_anthropic_provider_unconfigured_without_any_key(monkeypatch):
    _enable_anthropic_planner(monkeypatch, key="")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BROWSER_PLANNER_API_KEY", raising=False)
    assert multimodal.get_visual_planner() is None


def test_unknown_provider_fails_closed(monkeypatch):
    _enable_anthropic_planner(monkeypatch)
    monkeypatch.setattr(settings, "browser_planner_provider", "other")
    monkeypatch.setattr(settings, "browser_planner_model", "m")
    monkeypatch.setenv("BROWSER_PLANNER_API_KEY", "k")
    p = multimodal.MultimodalPlanner()
    with pytest.raises(multimodal.PlannerError):
        p._invoke({}, "g", b"\x89PNG", ("navigate",))


def test_media_type_sniffs_actual_bytes():
    assert multimodal._media_type(b"\x89PNG\r\n") == "image/png"
    assert multimodal._media_type(b"\xff\xd8\xff\xe0") == "image/jpeg"


def test_strip_fences_unwraps_markdown_json():
    fenced = "```json\n{\"a\": 1}\n```"
    assert json.loads(multimodal._strip_fences(fenced)) == {"a": 1}
    assert multimodal._strip_fences('{"a": 1}') == '{"a": 1}'


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeUsage:
    input_tokens = 11
    output_tokens = 7


class _FakeResponse:
    stop_reason = "end_turn"
    usage = _FakeUsage()

    def __init__(self, text):
        self.content = [_FakeBlock(text)]


def test_anthropic_invoke_parses_json_and_reports_usage(monkeypatch):
    _enable_anthropic_planner(monkeypatch)
    p = multimodal.MultimodalPlanner()
    captured = {}

    import anthropic

    class _FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _FakeResponse('```json\n{"operation": "navigate"}\n```')

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.messages = _FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
    raw, usage = p._invoke({"url": "https://example.com", "elements": []},
                           "goal", b"\x89PNG-bytes", ("navigate",))
    assert raw == {"operation": "navigate"}
    assert usage == {"input_tokens": 11, "output_tokens": 7}
    assert captured["model"] == "claude-opus-4-8"
    # The screenshot went as a base64 image block with a sniffed media type.
    img = captured["messages"][0]["content"][0]
    assert img["type"] == "image"
    assert img["source"]["media_type"] == "image/png"
    # The key stays inside the client construction, never in the message.
    assert captured["client_kwargs"]["api_key"] == "k"


def test_anthropic_invoke_maps_errors_and_never_leaks(monkeypatch):
    _enable_anthropic_planner(monkeypatch)
    p = multimodal.MultimodalPlanner()

    import anthropic
    import httpx

    def _raising_client(exc):
        class _M:
            def create(self, **kwargs):
                raise exc

        class _C:
            def __init__(self, **kwargs):
                self.messages = _M()

        return _C

    # Transport-shaped errors are retryable (mapped to _TransportError).
    conn_err = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com"))
    monkeypatch.setattr(anthropic, "Anthropic", _raising_client(conn_err))
    with pytest.raises(multimodal._TransportError):
        p._invoke_anthropic({}, "g", b"png", ("navigate",))

    # Anything else fails closed with the class name only — no payload/body.
    secret_exc = ValueError("body-with-secret sk-DO-NOT-LEAK")
    monkeypatch.setattr(anthropic, "Anthropic", _raising_client(secret_exc))
    with pytest.raises(multimodal.PlannerError) as ei:
        p._invoke_anthropic({}, "g", b"png", ("navigate",))
    assert "sk-DO-NOT-LEAK" not in str(ei.value)


def test_anthropic_refusal_fails_closed(monkeypatch):
    _enable_anthropic_planner(monkeypatch)
    p = multimodal.MultimodalPlanner()

    import anthropic

    class _Refused(_FakeResponse):
        stop_reason = "refusal"

    class _M:
        def create(self, **kwargs):
            return _Refused("")

    class _C:
        def __init__(self, **kwargs):
            self.messages = _M()

    monkeypatch.setattr(anthropic, "Anthropic", _C)
    with pytest.raises(multimodal.PlannerError):
        p._invoke_anthropic({}, "g", b"png", ("navigate",))


# ── browserbase contexts (saved logins) — payload contracts, no network ─────

def _bb_enabled(monkeypatch):
    monkeypatch.setattr(settings, "browser_real_provider_enabled", True)
    monkeypatch.setattr(settings, "browserbase_api_key", "k")
    monkeypatch.setattr(settings, "browserbase_project_id", "p")


class _FakeHTTPResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_browserbase_create_threads_context_into_payload(monkeypatch):
    _bb_enabled(monkeypatch)
    from app.browser import browserbase_provider as bb

    captured = {}

    import httpx

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _FakeHTTPResponse({"id": "s1", "connectUrl": "wss://x"})

    monkeypatch.setattr(httpx, "post", fake_post)
    prov = bb.BrowserbaseProvider()
    sid, _ = prov._create_remote_session(60, "ctx-123")
    assert sid == "s1"
    assert captured["json"]["browserSettings"]["context"] == {
        "id": "ctx-123", "persist": True}
    # Without a profile there is NO context key at all.
    prov._create_remote_session(60, "")
    assert "context" not in captured["json"]["browserSettings"]


def test_browserbase_create_context_and_viewer(monkeypatch):
    _bb_enabled(monkeypatch)
    from app.browser import browserbase_provider as bb

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, **kw: _FakeHTTPResponse(
        {"id": "ctx-9"}))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _FakeHTTPResponse(
        {"debuggerFullscreenUrl": "https://live.example/view"}))
    prov = bb.BrowserbaseProvider()
    assert prov.create_context() == "ctx-9"
    viewer = prov.viewer("sess-1")
    assert viewer == {"kind": "live", "url": "https://live.example/view"}
    # The operator's strict allowlist passes exactly these keys through.
    from app.browser.operator import _safe_viewer
    safe = _safe_viewer(viewer)
    assert safe["url"] == "https://live.example/view"
    assert safe["read_only"] is True
