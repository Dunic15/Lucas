"""Recall setup diagnostics. No API calls, no secrets needed."""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import recall_client  # noqa: E402
from app.config import settings  # noqa: E402


def test_readiness_rejects_webhook_secret_as_api_key(monkeypatch):
    monkeypatch.setattr(settings, "recall_api_key", "whsec_not_an_api_key")
    monkeypatch.setattr(settings, "public_base_url", "https://example.ngrok.app")

    status = recall_client.readiness()

    assert status["ready"] is False
    assert status["api_key"] == "webhook_secret"
    assert any("webhook verification secret" in issue for issue in status["issues"])


def test_readiness_requires_public_https_url(monkeypatch):
    monkeypatch.setattr(settings, "recall_api_key", "real-looking-key")
    monkeypatch.setattr(settings, "public_base_url", "http://127.0.0.1:8000")

    status = recall_client.readiness()

    assert status["ready"] is False
    assert status["public_base_url"] == "not_https"
    assert any("HTTPS URL" in issue for issue in status["issues"])


def test_readiness_accepts_api_key_and_public_https_url(monkeypatch):
    monkeypatch.setattr(settings, "recall_api_key", "real-looking-key")
    monkeypatch.setattr(settings, "public_base_url", "https://laura.ngrok.app")

    status = recall_client.readiness()

    assert status["ready"] is True
    assert status["api_key"] == "set"
    assert status["public_base_url"] == "set"


def test_verify_webhook_accepts_matching_signature(monkeypatch):
    secret_key = base64.b64encode(b"test-secret").decode()
    monkeypatch.setattr(settings, "recall_webhook_secret", f"whsec_{secret_key}")
    body = b'{"event":"transcript.data"}'
    msg_id = "msg_123"
    timestamp = "1234567890"
    signed_payload = b".".join([msg_id.encode(), timestamp.encode(), body])
    sig = base64.b64encode(
        hmac.new(b"test-secret", signed_payload, hashlib.sha256).digest()
    ).decode()

    recall_client.verify_webhook(
        body,
        {
            "webhook-id": msg_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": f"v1,{sig}",
        },
    )


def test_verify_webhook_rejects_bad_signature(monkeypatch):
    secret_key = base64.b64encode(b"test-secret").decode()
    monkeypatch.setattr(settings, "recall_webhook_secret", f"whsec_{secret_key}")

    try:
        recall_client.verify_webhook(
            b'{"event":"transcript.data"}',
            {
                "webhook-id": "msg_123",
                "webhook-timestamp": "1234567890",
                "webhook-signature": "v1,bm90LXRoZS1zaWduYXR1cmU=",
            },
        )
    except RuntimeError as e:
        assert "verification failed" in str(e)
    else:
        raise AssertionError("bad webhook signature was accepted")


def test_request_retries_retryable_status(monkeypatch):
    request = httpx.Request("GET", "https://example.test")
    responses = [
        httpx.Response(429, request=request),
        httpx.Response(200, request=request),
    ]
    calls = []

    class FakeClient:
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return responses.pop(0)

    monkeypatch.setattr(recall_client, "_CLIENT", FakeClient())
    monkeypatch.setattr(recall_client.time, "sleep", lambda delay: None)

    resp = recall_client._request("GET", "https://example.test", retry=True)

    assert resp.status_code == 200
    assert len(calls) == 2


def test_create_bot_uses_default_recallai_low_latency_transcription(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://laura.example")
    monkeypatch.setattr(settings, "recall_api_base", "https://eu-central-1.recall.ai")
    monkeypatch.setattr(settings, "recall_transcription_provider", "recallai")
    monkeypatch.setattr(settings, "recall_transcription_mode", "prioritize_low_latency")
    monkeypatch.setattr(settings, "recall_transcription_language_code", "en")
    monkeypatch.setattr(recall_client, "_headers", lambda: {"Authorization": "key"})

    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        request = httpx.Request(method, url)
        return httpx.Response(201, json={"id": "bot_1"}, request=request)

    monkeypatch.setattr(recall_client, "_request", fake_request)

    recall_client.create_bot(
        "https://meet.google.com/abc-defg-hij",
        "https://laura.example/avatar",
    )

    provider = captured["json"]["recording_config"]["transcript"]["provider"]
    assert provider == {
        "recallai_streaming": {
            "mode": "prioritize_low_latency",
            "language_code": "en",
        }
    }


def test_create_bot_can_use_elevenlabs_streaming_transcription(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://laura.example")
    monkeypatch.setattr(settings, "recall_api_base", "https://eu-central-1.recall.ai")
    monkeypatch.setattr(settings, "recall_transcription_provider", "elevenlabs")
    monkeypatch.setattr(settings, "elevenlabs_transcription_model", "scribe_v2_realtime")
    monkeypatch.setattr(settings, "elevenlabs_transcription_language_code", "")
    monkeypatch.setattr(recall_client, "_headers", lambda: {"Authorization": "key"})

    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        request = httpx.Request(method, url)
        return httpx.Response(201, json={"id": "bot_1"}, request=request)

    monkeypatch.setattr(recall_client, "_request", fake_request)

    recall_client.create_bot(
        "https://meet.google.com/abc-defg-hij",
        "https://laura.example/avatar",
    )

    provider = captured["json"]["recording_config"]["transcript"]["provider"]
    assert provider == {
        "elevenlabs_streaming": {
            "model_id": "scribe_v2_realtime",
        }
    }
