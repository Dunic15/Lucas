"""Product analytics (PostHog server-side): OFF by default, fire-and-forget,
ids/counters only. Key-free — httpx is monkeypatched, no network."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import settings
from app.integrations import product_analytics


def test_off_by_default_is_a_noop(monkeypatch):
    monkeypatch.setattr(settings, "posthog_api_key", "")
    spawned = []
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: spawned.append(1))
    product_analytics.capture("user_signed_up", "u1", {"org_id": "o1"})
    assert not spawned  # no thread, no network, byte-identical demo


def test_capture_posts_ids_only_payload(monkeypatch):
    monkeypatch.setattr(settings, "posthog_api_key", "phc_test")
    monkeypatch.setattr(settings, "posthog_host", "https://eu.i.posthog.com/")
    sent = {}

    class _T:
        def __init__(self, target=None, args=(), daemon=None):
            self._target, self._args = target, args

        def start(self):  # run inline so the test can assert
            self._target(*self._args)

    monkeypatch.setattr(threading, "Thread", _T)

    def fake_post(url, json=None, timeout=None):
        sent.update(url=url, payload=json, timeout=timeout)

    monkeypatch.setattr(httpx, "post", fake_post)
    product_analytics.capture(
        "meeting_finalized", "org1", {"avatar_id": "laura", "actions": 3})
    assert sent["url"] == "https://eu.i.posthog.com/capture/"
    assert sent["payload"]["event"] == "meeting_finalized"
    assert sent["payload"]["distinct_id"] == "org1"
    assert sent["payload"]["properties"] == {"avatar_id": "laura", "actions": 3}
    assert sent["payload"]["api_key"] == "phc_test"


def test_capture_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "posthog_api_key", "phc_test")

    class _T:
        def __init__(self, target=None, args=(), daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(threading, "Thread", _T)

    def boom(*a, **k):
        raise RuntimeError("posthog down")

    monkeypatch.setattr(httpx, "post", boom)
    product_analytics.capture("user_logged_in", "u1")  # must not raise
    # Empty event / distinct_id are dropped, not sent.
    product_analytics.capture("", "u1")
    product_analytics.capture("x", "")
