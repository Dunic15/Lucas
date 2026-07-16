"""Per-org Google access-token cache + refresh-token rotation. Google mocked.

Before the cache, EVERY google_client call paid a token round-trip; and a
rotated refresh_token in Google's response was silently discarded, stranding
the org on a revoked credential. The conftest autouse reset clears the cache
between tests (it is deliberate process-global state).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store  # noqa: E402
from app.config import settings  # noqa: E402


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._p = payload

    def json(self) -> dict:
        return self._p


def _mock(monkeypatch, tmp_path, *, token_payload=None, gmail_status=200):
    """google_client with org-a connected; counts token mints and API calls."""
    from app import google_client

    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    monkeypatch.setattr(settings, "session_secret", "sek")
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csec")
    store.set_org_oauth("org-a", "rt-1")
    counts = {"token": 0, "api": 0}
    payload = token_payload or {"access_token": "at-1", "expires_in": 3599}

    def fake_post(url, **kw):
        if url.endswith("/token"):
            counts["token"] += 1
            return _Resp(200, dict(payload))
        counts["api"] += 1
        return _Resp(gmail_status, {"id": "m1", "threadId": "t1"})

    monkeypatch.setattr(google_client.httpx, "post", fake_post)
    return google_client, counts


_MSG = {"to": "a@b.com", "subject": "s", "body": "b"}


def test_second_call_reuses_cached_token(monkeypatch, tmp_path):
    gc, counts = _mock(monkeypatch, tmp_path)
    assert gc.send_gmail("org-a", _MSG)["ok"]
    assert gc.send_gmail("org-a", _MSG)["ok"]
    assert counts == {"token": 1, "api": 2}  # one mint, two sends


def test_expired_entry_reminents(monkeypatch, tmp_path):
    gc, counts = _mock(monkeypatch, tmp_path)
    assert gc.send_gmail("org-a", _MSG)["ok"]
    with gc._TOKEN_LOCK:  # age the entry past its expiry
        tok, _ = gc._TOKEN_CACHE["org-a"]
        gc._TOKEN_CACHE["org-a"] = (tok, 0.0)
    assert gc.send_gmail("org-a", _MSG)["ok"]
    assert counts["token"] == 2


def test_cache_is_per_org(monkeypatch, tmp_path):
    gc, counts = _mock(monkeypatch, tmp_path)
    store.set_org_oauth("org-b", "rt-b")
    assert gc.send_gmail("org-a", _MSG)["ok"]
    assert gc.send_gmail("org-b", _MSG)["ok"]
    assert counts["token"] == 2  # no cross-org sharing


def test_api_401_drops_cache_and_next_call_reminents(monkeypatch, tmp_path):
    gc, counts = _mock(monkeypatch, tmp_path, gmail_status=401)
    assert gc.send_gmail("org-a", _MSG)["ok"] is False  # revoked mid-hour
    assert gc.send_gmail("org-a", _MSG)["ok"] is False
    assert counts["token"] == 2  # the 401 dropped the entry → re-mint


def test_rotated_refresh_token_is_persisted(monkeypatch, tmp_path):
    gc, counts = _mock(
        monkeypatch, tmp_path,
        token_payload={"access_token": "at-1", "expires_in": 3599,
                       "refresh_token": "rt-2"},
    )
    assert gc.send_gmail("org-a", _MSG)["ok"]
    # Google rotated the refresh token → the stored row now carries rt-2, so
    # the NEXT process boot (or cache expiry) refreshes with the live one.
    assert store.get_org_oauth("org-a")["refresh_token"] == "rt-2"


def test_unrotated_refresh_token_is_not_rewritten(monkeypatch, tmp_path):
    gc, _ = _mock(monkeypatch, tmp_path)
    before = store.get_org_oauth("org-a")["updated_at"]
    assert gc.send_gmail("org-a", _MSG)["ok"]
    assert store.get_org_oauth("org-a")["refresh_token"] == "rt-1"
    assert store.get_org_oauth("org-a")["updated_at"] == before  # no churn
