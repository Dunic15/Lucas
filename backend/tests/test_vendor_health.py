"""Vendor subscription watchdog: thresholds, off-when-unconfigured, Slack text.
No keys, no network — every HTTP call is stubbed."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import vendor_health  # noqa: E402
from app.config import settings  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _stub_client(monkeypatch, resp: _Resp):
    monkeypatch.setattr(
        vendor_health,
        "_client",
        type("C", (), {"get": staticmethod(lambda *a, **k: resp),
                       "post": staticmethod(lambda *a, **k: resp)}),
    )


# ── off when unconfigured (zero-key demo stays quiet) ──


def test_unconfigured_vendors_are_off(monkeypatch):
    for field in ("elevenlabs_api_key", "recall_api_key", "groq_api_key",
                  "anthropic_api_key", "runpod_api_key"):
        monkeypatch.setattr(settings, field, "")
    for check in (vendor_health.check_elevenlabs, vendor_health.check_recall,
                  vendor_health.check_groq, vendor_health.check_anthropic,
                  vendor_health.check_runpod):
        assert check()["status"] == "off", check.__name__


# ── ElevenLabs thresholds ──


def _el(monkeypatch, used, limit):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "k")
    _stub_client(monkeypatch, _Resp(200, {
        "character_count": used, "character_limit": limit,
        "next_character_count_reset_unix": 1790000000,
    }))
    return vendor_health.check_elevenlabs()


def test_elevenlabs_ok_warn_crit(monkeypatch):
    assert _el(monkeypatch, 100_000, 1_000_000)["status"] == "ok"      # 90% left
    assert _el(monkeypatch, 950_000, 1_000_000)["status"] == "warn"    # 5% left
    assert _el(monkeypatch, 990_000, 1_000_000)["status"] == "crit"    # 1% left


def test_elevenlabs_invalid_key_is_crit(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "k")
    _stub_client(monkeypatch, _Resp(401))
    assert vendor_health.check_elevenlabs()["status"] == "crit"


# ── RunPod balance thresholds ──


def _rp(monkeypatch, balance):
    monkeypatch.setattr(settings, "runpod_api_key", "k")
    _stub_client(monkeypatch, _Resp(200, {"data": {"myself": {"clientBalance": balance}}}))
    return vendor_health.check_runpod()


def test_runpod_ok_warn_crit(monkeypatch):
    assert _rp(monkeypatch, 25.0)["status"] == "ok"
    assert _rp(monkeypatch, 4.0)["status"] == "warn"
    assert _rp(monkeypatch, 1.0)["status"] == "crit"


# ── Google refresh token: the silent killer ──


def test_google_refresh_failure_is_crit(monkeypatch):
    from app import gmail_watcher

    monkeypatch.setattr(gmail_watcher, "refresh_token", lambda: "rt")

    def boom(rt):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(gmail_watcher, "access_token", boom)
    r = vendor_health.check_google_oauth()
    assert r["status"] == "crit"
    assert "oauth/google/connect" in r["detail"]


def test_google_not_connected_is_off(monkeypatch):
    from app import gmail_watcher

    monkeypatch.setattr(gmail_watcher, "refresh_token", lambda: "")
    assert vendor_health.check_google_oauth()["status"] == "off"


# ── vendor DOWN never raises: the watchdog needs no watchdog ──


def test_network_error_degrades_to_warn(monkeypatch):
    monkeypatch.setattr(settings, "recall_api_key", "k")
    monkeypatch.setattr(
        vendor_health, "_client",
        type("C", (), {"get": staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))}),
    )
    assert vendor_health.check_recall()["status"] == "warn"


# ── Slack text: actionable items only, silent when all green ──


def test_slack_text_empty_when_all_ok():
    results = [{"service": "x", "status": "ok", "detail": "d"},
               {"service": "y", "status": "off", "detail": "d"}]
    assert vendor_health.slack_text(results) == ""


def test_slack_text_lists_bad_crit_first():
    results = [
        {"service": "elevenlabs", "status": "warn", "detail": "5% rimasti"},
        {"service": "google-oauth", "status": "crit", "detail": "refresh FALLITO"},
        {"service": "recall", "status": "ok", "detail": "ok"},
    ]
    text = vendor_health.slack_text(results)
    assert "google-oauth" in text and "elevenlabs" in text
    assert text.index("google-oauth") < text.index("elevenlabs")  # crit first
    assert "recall" not in text.replace("(altri", "")  # ok items summarized only
