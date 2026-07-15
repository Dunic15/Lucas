"""Native Google executor slice — crypto, per-org token store, google_client
(mocked Google), and the executor dispatch + ledger provenance.

Everything here is key-free: Google is mocked, native_executor is toggled per
test, and the store is a fresh temp SQLite. The flag stays OFF by default so the
rest of the suite (and the demo) is untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store  # noqa: E402
from app.config import settings  # noqa: E402


def _fresh_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()


# ── crypto ──

def test_crypto_roundtrip_and_tamper():
    from app import crypto

    for s in ["", "a", "1//refresh-token-abc123", "unicode ✓ é", "x" * 800]:
        assert crypto.decrypt(crypto.encrypt(s, "key"), "key") == s

    tok = crypto.encrypt("super-secret-token", "k1")
    assert "super-secret-token" not in tok  # not plaintext at rest
    with pytest.raises(ValueError):
        crypto.decrypt(tok, "k2")  # wrong key
    with pytest.raises(ValueError):
        crypto.decrypt(tok[:-6] + "AAAAAA", "k1")  # tampered ciphertext


def test_crypto_new_tokens_are_fernet():
    """New tokens use the vetted Fernet cipher (not the hand-rolled scheme)."""
    from app import crypto

    assert crypto.encrypt("x", "k").startswith("gAAAAA")  # Fernet version prefix


def test_crypto_reads_legacy_tokens():
    """decrypt() still reads tokens written by the pre-Fernet HMAC-CTR scheme, so
    an in-place upgrade never invalidates an already-stored refresh token."""
    import base64
    import hashlib
    import hmac
    import os

    from app import crypto

    secret = "k1"
    enc_key, mac_key = crypto._derive(secret)
    nonce = os.urandom(16)
    pt = b"1//legacy-refresh-token"
    ct = bytes(a ^ b for a, b in zip(pt, crypto._keystream(enc_key, nonce, len(pt))))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    legacy = base64.urlsafe_b64encode(nonce + tag + ct).decode()

    assert crypto.decrypt(legacy, secret) == "1//legacy-refresh-token"
    with pytest.raises(ValueError):
        crypto.decrypt(legacy, "wrong-key")  # legacy token, wrong key


# ── per-org token store ──

def test_org_oauth_roundtrip_and_encryption(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "google_token_enc_key", "")
    monkeypatch.setattr(settings, "session_secret", "sek")

    assert store.set_org_oauth("org-a", "rt-123", email="Owner@Foo.com", scopes="a b")
    got = store.get_org_oauth("org-a")
    assert got and got["refresh_token"] == "rt-123"
    assert got["email"] == "owner@foo.com" and got["scopes"] == "a b"

    with store._connect() as c:
        raw = c.execute(
            "SELECT refresh_token_enc FROM org_oauth WHERE org_id='org-a'"
        ).fetchone()[0]
    assert raw and "rt-123" not in raw  # stored ciphertext, never plaintext

    assert store.get_org_oauth("unknown-org") is None
    assert store.set_org_oauth("", "rt") is False
    assert store.set_org_oauth("org-b", "") is False

    # Rotating the encryption key makes an old token undecryptable → reads as
    # "not connected" (reconnect), never a crash or a wrong token.
    monkeypatch.setattr(settings, "session_secret", "rotated")
    assert store.get_org_oauth("org-a") is None


def test_org_oauth_fails_closed_without_key(monkeypatch, tmp_path):
    """With neither GOOGLE_TOKEN_ENC_KEY nor SESSION_SECRET set, refuse to store a
    token under a public default: the write fails closed and a read is a clean
    'not connected' (never a plaintext token, never a crash)."""
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "google_token_enc_key", "")
    monkeypatch.setattr(settings, "session_secret", "")

    with pytest.raises(RuntimeError):
        store.set_org_oauth("org-x", "rt-999")
    assert store.get_org_oauth("org-x") is None


# ── google_client (Google mocked) ──

class _Resp:
    def __init__(self, code: int, payload: dict):
        self.status_code = code
        self._p = payload

    def json(self) -> dict:
        return self._p


def _mock_google(monkeypatch, *, token=("at-1", 200), cal=None, gmail=None):
    from app import google_client

    monkeypatch.setattr(settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csec")
    cal = cal if cal is not None else (
        200,
        {"id": "ev1", "htmlLink": "https://cal/ev1",
         "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc"},
    )
    gmail = gmail if gmail is not None else (200, {"id": "m1", "threadId": "t1"})
    calls: list[str] = []

    def fake_post(url, **kw):
        calls.append(url)
        if url.endswith("/token"):
            at, code = token
            return _Resp(code, {"access_token": at} if at else {})
        if "calendar" in url:
            return _Resp(*cal)
        if "gmail" in url:
            return _Resp(*gmail)
        return _Resp(404, {})

    monkeypatch.setattr(google_client.httpx, "post", fake_post)
    return google_client, calls


def test_google_client_happy_path(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "session_secret", "sek")
    store.set_org_oauth("org-a", "rt-123")
    gc, calls = _mock_google(monkeypatch)

    ev = gc.create_calendar_event(
        "org-a",
        {"title": "Follow-up", "start": "2026-08-01T10:00:00",
         "end": "2026-08-01T10:30:00", "attendees": ["a@b.com", "c@d.com"]},
    )
    assert ev == {
        "ok": True,
        "event_id": "ev1",
        "event_url": "https://cal/ev1",
        "meet_url": "https://meet.google.com/aaa-bbbb-ccc",
    }

    msg = gc.send_gmail("org-a", {"to": "a@b.com", "subject": "Recap", "body": "Notes"})
    assert msg["ok"] and msg["message_id"] == "m1"
    assert any("calendar" in u for u in calls) and any("gmail" in u for u in calls)


def test_google_client_soft_failures(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "session_secret", "sek")
    gc, _ = _mock_google(monkeypatch)

    # No token stored for the org → soft "not connected", no exception.
    r = gc.create_calendar_event("no-token", {"title": "x", "start": "s", "end": "e"})
    assert r["ok"] is False and "not connected" in r["error"]

    # Missing required fields → soft error before any HTTP.
    store.set_org_oauth("org-a", "rt")
    assert gc.send_gmail("org-a", {"subject": "only"})["ok"] is False
    assert gc.create_calendar_event("org-a", {"title": "no times"})["ok"] is False

    # Google 5xx → ok False, never raises.
    gc2, _ = _mock_google(monkeypatch, cal=(503, {}))
    assert gc2.create_calendar_event(
        "org-a", {"title": "t", "start": "s", "end": "e"}
    )["ok"] is False

    # Token refresh rejected → soft error.
    gc3, _ = _mock_google(monkeypatch, token=("", 400))
    assert gc3.send_gmail("org-a", {"to": "a@b.com", "subject": "s"})["ok"] is False


# ── google_client.list_calendar_events (read side, Google mocked) ──

def test_list_calendar_events_happy_and_soft(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "session_secret", "sek")
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csec")
    from app import google_client

    store.set_org_oauth("org-a", "rt-123")

    seen: dict = {}

    def fake_get(url, **kw):
        seen.update(kw.get("params") or {})
        return _Resp(200, {"items": [{"id": "e1", "summary": "Sync"}]})

    monkeypatch.setattr(google_client.httpx, "post", lambda url, **kw: _Resp(200, {"access_token": "at-1"}))
    monkeypatch.setattr(google_client.httpx, "get", fake_get)

    res = google_client.list_calendar_events("org-a", max_results=25)
    assert res["ok"] is True and res["events"][0]["id"] == "e1"
    # Upcoming-only, expanded, chronological — the read-side query contract.
    assert seen["singleEvents"] == "true" and seen["orderBy"] == "startTime"
    assert "timeMin" in seen and seen["maxResults"] == 25

    # No token for the org → soft "not connected", never raises.
    assert google_client.list_calendar_events("no-token")["ok"] is False

    # Google 5xx → ok False, never raises.
    monkeypatch.setattr(google_client.httpx, "get", lambda url, **kw: _Resp(503, {}))
    assert google_client.list_calendar_events("org-a")["ok"] is False

    # Transport blow-up → ok False, never raises.
    def boom(url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(google_client.httpx, "get", boom)
    assert google_client.list_calendar_events("org-a")["ok"] is False


# ── executor (google_client mocked) ──

def test_executor_off_is_noop(monkeypatch):
    from app import executor

    monkeypatch.setattr(settings, "native_executor", False)
    assert executor.enabled() is False
    assert executor.handles({"type": "email.send"}) is False
    r = executor.execute_approved("org-a", "aid", {"type": "email.send"})
    assert r["ok"] is False and r["skipped"] == "native_executor off"


def test_executor_dispatch_and_provenance(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    from app import executor

    monkeypatch.setattr(settings, "native_executor", True)
    statuses: list[tuple] = []

    def fake_status(action_id, status, detail="", *, org_id=""):
        statuses.append((action_id, status, detail, org_id))
        return True

    monkeypatch.setattr(executor.ledger, "set_action_status", fake_status)
    monkeypatch.setattr(
        executor.google_client, "create_calendar_event",
        lambda org, ev: {"ok": True, "event_url": "https://cal/ev1", "event_id": "ev1"},
    )
    monkeypatch.setattr(
        executor.google_client, "send_gmail",
        lambda org, msg: {"ok": True, "message_id": "m1"},
    )

    assert executor.handles({"type": "calendar.create_event"}) is True

    r = executor.execute_approved(
        "org-a", "act-1", {"type": "calendar.create_event", "event": {"title": "x"}}
    )
    assert r["ok"] is True
    assert statuses[-1][:2] == ("act-1", "done")
    assert "https://cal/ev1" in statuses[-1][2] and statuses[-1][3] == "org-a"

    executor.execute_approved("org-a", "act-2", {"type": "email.send", "message": {}})
    assert statuses[-1][:2] == ("act-2", "done") and "m1" in statuses[-1][2]

    # A soft failure records a "failed" receipt with the reason.
    monkeypatch.setattr(
        executor.google_client, "send_gmail",
        lambda org, msg: {"ok": False, "error": "gmail send failed (HTTP 500)"},
    )
    r3 = executor.execute_approved("org-a", "act-3", {"type": "email.send"})
    assert r3["ok"] is False
    assert statuses[-1][:2] == ("act-3", "failed") and "500" in statuses[-1][2]

    # An unhandled action type is skipped with NO ledger write.
    before = len(statuses)
    skipped = executor.execute_approved("org-a", "act-4", {"type": "slack.post"})
    assert skipped.get("skipped") and len(statuses) == before


def test_executor_closes_real_ledger_row(monkeypatch, tmp_path):
    """End-to-end against the REAL ledger: an approved action's execution closes
    its ledger row and records the receipt in the existing status channel."""
    _fresh_store(monkeypatch, tmp_path)
    from app import executor, ledger

    ledger._init_db()  # ledger_items / action_status live in the same sqlite file
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(
        executor.google_client, "send_gmail",
        lambda org, msg: {"ok": True, "message_id": "m-real"},
    )
    org = settings.demo_org_id
    aid = ledger.new_action_id()
    ledger.record_meeting(
        "https://meet.google.com/abc-defg-hij", "laura", "bot-1",
        {"summary": "s", "actions": [{"item": "email the recap", "action_id": aid}]},
        org_id=org,
    )
    r = executor.execute_approved(org, aid, {"type": "email.send", "message": {"to": "x@y.com", "subject": "s"}})
    assert r["ok"] is True
    st = ledger.get_action_status(aid, org_id=org) if hasattr(ledger, "get_action_status") else None
    # The ledger item is now closed (done) regardless of the status-read helper.
    with store._connect() as c:
        row = c.execute(
            "SELECT status FROM ledger_items WHERE action_id=? AND org_id=?",
            (aid, org),
        ).fetchone()
    assert row is not None and row["status"] == "done"
