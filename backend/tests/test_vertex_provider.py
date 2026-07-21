"""BRAIN_PROVIDER=vertex: Gemini via Vertex AI.

Key-free: the access token and the HTTP call are both mocked, so nothing here
touches google-auth or the network. Verifies request shaping (host derived from
the location, model/project in the path, bearer + systemInstruction), response
parsing, the missing-project guard, and that the provider is wired into the
dispatch + streaming paths.
"""
from __future__ import annotations

import httpx
import pytest

from app import llm
from app.config import settings


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _mock_post(monkeypatch, payload, captured):
    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        captured["json"] = kwargs.get("json", {})
        return _FakeResp(payload)

    monkeypatch.setattr(httpx, "post", fake_post)


def test_vertex_complete_parses_text_and_shapes_request(monkeypatch):
    monkeypatch.setattr(settings, "vertex_project", "proj-123")
    monkeypatch.setattr(settings, "vertex_location", "global")
    monkeypatch.setattr(settings, "vertex_model", "gemini-3.5-flash")
    monkeypatch.setattr(llm, "_vertex_token", lambda: "fake-token")
    captured: dict = {}
    _mock_post(
        monkeypatch,
        {"candidates": [{"content": {"parts": [{"text": "ci"}, {"text": "ao"}]}}]},
        captured,
    )

    out = llm.complete("SYS", "hi there", provider="vertex")

    assert out == "ciao"  # parts are concatenated
    assert "aiplatform.googleapis.com/v1/projects/proj-123" in captured["url"]
    assert "locations/global/publishers/google/models/gemini-3.5-flash:generateContent" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer fake-token"
    assert captured["json"]["systemInstruction"]["parts"][0]["text"] == "SYS"
    assert captured["json"]["contents"][0]["parts"][0]["text"] == "hi there"


def test_vertex_region_uses_region_host(monkeypatch):
    monkeypatch.setattr(settings, "vertex_project", "p")
    monkeypatch.setattr(settings, "vertex_location", "us-central1")
    monkeypatch.setattr(settings, "vertex_model", "gemini-2.5-flash")
    monkeypatch.setattr(llm, "_vertex_token", lambda: "t")
    captured: dict = {}
    _mock_post(monkeypatch, {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}, captured)

    llm._complete_vertex("", "hi", 100, None)

    assert captured["url"].startswith("https://us-central1-aiplatform.googleapis.com/")
    assert "locations/us-central1/" in captured["url"]
    # no system -> no systemInstruction key
    assert "systemInstruction" not in captured["json"]


def test_vertex_no_candidates_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "vertex_project", "p")
    monkeypatch.setattr(llm, "_vertex_token", lambda: "t")
    _mock_post(monkeypatch, {"candidates": []}, {})
    assert llm._complete_vertex("", "hi", 100, None) == ""


def test_vertex_requires_project(monkeypatch):
    monkeypatch.setattr(settings, "vertex_project", "")
    monkeypatch.setattr(llm, "_vertex_token", lambda: "t")
    with pytest.raises(RuntimeError, match="VERTEX_PROJECT"):
        llm._complete_vertex("", "hi", 100, None)


def test_vertex_streaming_yields_single_chunk(monkeypatch):
    # stream_complete routes non-OpenAI-compat providers through complete(),
    # which dispatches to _complete_vertex. With brain_provider=vertex the whole
    # answer arrives as one chunk (correct, just not early-streamed).
    monkeypatch.setattr(settings, "brain_provider", "vertex")
    monkeypatch.setattr(llm, "_complete_vertex", lambda system, user, mt, model=None: "full answer")

    chunks = list(llm.stream_complete("SYS", "hi"))

    assert chunks == ["full answer"]


def test_vertex_token_missing_google_auth_is_clear(monkeypatch):
    # If google-auth isn't installed, the error names the fix (not an ImportError
    # deep in a call stack). Simulate by clearing the cache and blocking the import.
    monkeypatch.setattr(llm, "_vertex_token_cache", {"tok": "", "exp": 0.0})
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "google.auth" or name.startswith("google.auth"):
            raise ImportError("no google.auth")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(RuntimeError, match="google-auth"):
        llm._vertex_token()
