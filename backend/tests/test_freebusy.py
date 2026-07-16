"""google_client.freebusy(): status mapping + token self-heal, all with httpx
monkeypatched. Never touches the network; never raises."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import google_client


class _Resp:
    def __init__(self, code, payload):
        self.status_code = code
        self._payload = payload

    def json(self):
        return self._payload


def _tok(monkeypatch, calls=None):
    def fake_access(principal, oauth=None, *, on_rotate=None, force_refresh=False):
        if calls is not None:
            calls.append("refresh" if force_refresh else "get")
        return ("tok-fresh" if force_refresh else "tok-1"), ""
    monkeypatch.setattr(google_client, "_access_token", fake_access)


def test_status_mapping(monkeypatch):
    _tok(monkeypatch)
    payload = {"calendars": {
        "primary": {"busy": [{"start": "2026-07-20T10:00:00Z", "end": "2026-07-20T11:00:00Z"}]},
        "ananth@x.com": {"errors": [{"reason": "notFound"}]},
        # 'ghost@x.com' intentionally NOT returned by Google
    }}
    monkeypatch.setattr(google_client.httpx, "post", lambda *a, **k: _Resp(200, payload))
    r = google_client.freebusy("org-1", ["primary", "ananth@x.com", "ghost@x.com"],
                               "2026-07-20T00:00:00", "2026-07-27T00:00:00")
    assert r["ok"] is True
    assert r["calendars"]["primary"]["status"] == "readable"
    assert r["calendars"]["primary"]["busy"][0]["start"].startswith("2026-07-20T10")
    assert r["calendars"]["ananth@x.com"]["status"] == "no_permission"
    assert r["calendars"]["ghost@x.com"]["status"] == "no_account"


def test_403_triggers_one_force_refresh(monkeypatch):
    calls = []
    _tok(monkeypatch, calls)
    seq = [_Resp(403, {}), _Resp(200, {"calendars": {"primary": {"busy": []}}})]
    monkeypatch.setattr(google_client, "_drop_cached_token", lambda k: None)
    monkeypatch.setattr(google_client.httpx, "post", lambda *a, **k: seq.pop(0))
    r = google_client.freebusy("org-1", ["primary"], "s", "e")
    assert r["ok"] is True and r["calendars"]["primary"]["status"] == "readable"
    assert "refresh" in calls  # self-healed once


def test_never_raises_on_network_error(monkeypatch):
    _tok(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("connreset")
    monkeypatch.setattr(google_client.httpx, "post", boom)
    r = google_client.freebusy("org-1", ["primary"], "s", "e")
    assert r["ok"] is False and r["calendars"] == {}


def test_token_error_degrades(monkeypatch):
    monkeypatch.setattr(
        google_client, "_access_token",
        lambda p, o=None, *, on_rotate=None, force_refresh=False: ("", "connect_google"))
    r = google_client.freebusy("org-1", ["primary"], "s", "e")
    assert r["ok"] is False and r["error"] == "connect_google"


def test_empty_calendar_list_is_noop(monkeypatch):
    _tok(monkeypatch)
    r = google_client.freebusy("org-1", [], "s", "e")
    assert r["ok"] is True and r["calendars"] == {}
