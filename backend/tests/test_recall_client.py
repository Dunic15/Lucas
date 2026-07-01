"""Recall setup diagnostics. No API calls, no secrets needed."""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys
from pathlib import Path

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
    monkeypatch.setattr(settings, "public_base_url", "https://lucas.ngrok.app")

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
