"""send_gmail extras: thread replies, CC/BCC, HTML bodies. Google mocked.

The mock captures the exact JSON payload send_gmail would POST to Gmail and
the tests parse the raw MIME back out of it — asserting what Gmail would
actually receive, not what we hoped we built. The plain {to, subject, body}
call must stay byte-compatible with the pre-feature client.
"""
from __future__ import annotations

import base64
import email
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store  # noqa: E402
from app.config import settings  # noqa: E402


def _fresh_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._p = payload

    def json(self) -> dict:
        return self._p


def _mock_gmail(monkeypatch, tmp_path):
    """google_client with the org connected and Google mocked; returns the
    module plus the list of captured POST payloads (the Gmail one is last)."""
    from app import google_client

    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "session_secret", "sek")
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csec")
    store.set_org_oauth("org-a", "rt-123")
    payloads: list[dict] = []

    def fake_post(url, **kw):
        if url.endswith("/token"):
            return _Resp(200, {"access_token": "at-1"})
        payloads.append(kw.get("json") or {})
        return _Resp(200, {"id": "m1", "threadId": "t1"})

    monkeypatch.setattr(google_client.httpx, "post", fake_post)
    return google_client, payloads


def _sent_mime(payloads: list[dict]) -> email.message.Message:
    raw = payloads[-1]["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


def test_plain_send_unchanged(monkeypatch, tmp_path):
    gc, payloads = _mock_gmail(monkeypatch, tmp_path)
    r = gc.send_gmail("org-a", {"to": "a@b.com", "subject": "Recap", "body": "Notes"})
    assert r["ok"] and r["message_id"] == "m1"

    msg = _sent_mime(payloads)
    assert msg["To"] == "a@b.com" and msg["Subject"] == "Recap"
    assert not msg.is_multipart()
    assert msg.get_payload(decode=True).decode() == "Notes"
    assert "threadId" not in payloads[-1]  # no thread_id → plain send
    for h in ("Cc", "Bcc", "In-Reply-To", "References"):
        assert msg[h] is None


def test_cc_and_bcc_headers(monkeypatch, tmp_path):
    gc, payloads = _mock_gmail(monkeypatch, tmp_path)
    r = gc.send_gmail(
        "org-a",
        {"to": ["a@b.com"], "cc": "c@d.com; e@f.com", "bcc": ["g@h.com"],
         "subject": "Recap", "body": "Notes"},
    )
    assert r["ok"]
    msg = _sent_mime(payloads)
    assert msg["Cc"] == "c@d.com, e@f.com"
    assert msg["Bcc"] == "g@h.com"  # Gmail strips it on delivery


def test_html_body_is_multipart_alternative(monkeypatch, tmp_path):
    gc, payloads = _mock_gmail(monkeypatch, tmp_path)
    r = gc.send_gmail(
        "org-a",
        {"to": "a@b.com", "subject": "Recap", "body": "Plain notes",
         "html_body": "<p>Rich <b>notes</b></p>"},
    )
    assert r["ok"]
    msg = _sent_mime(payloads)
    assert msg.is_multipart() and msg.get_content_type() == "multipart/alternative"
    parts = {p.get_content_type(): p.get_payload(decode=True).decode()
             for p in msg.get_payload()}
    assert parts["text/plain"] == "Plain notes"
    assert "<b>notes</b>" in parts["text/html"]


def test_thread_reply_carries_threadid_and_rfc822_headers(monkeypatch, tmp_path):
    gc, payloads = _mock_gmail(monkeypatch, tmp_path)
    r = gc.send_gmail(
        "org-a",
        {"to": "a@b.com", "subject": "Re: Kickoff", "body": "Following up",
         "thread_id": "t-999", "in_reply_to": "CAF+xyz@mail.gmail.com"},
    )
    assert r["ok"]
    assert payloads[-1]["threadId"] == "t-999"  # Gmail-side threading
    msg = _sent_mime(payloads)
    # Client-side threading: angle brackets added if the caller omitted them.
    assert msg["In-Reply-To"] == "<CAF+xyz@mail.gmail.com>"
    assert msg["References"] == "<CAF+xyz@mail.gmail.com>"


def test_in_reply_to_already_bracketed_is_untouched(monkeypatch, tmp_path):
    gc, payloads = _mock_gmail(monkeypatch, tmp_path)
    gc.send_gmail(
        "org-a",
        {"to": "a@b.com", "subject": "Re: x", "body": "y",
         "in_reply_to": "<already@bracketed>"},
    )
    assert _sent_mime(payloads)["In-Reply-To"] == "<already@bracketed>"


def test_html_only_email_is_allowed(monkeypatch, tmp_path):
    gc, payloads = _mock_gmail(monkeypatch, tmp_path)
    r = gc.send_gmail("org-a", {"to": "a@b.com", "html_body": "<p>hi</p>"})
    assert r["ok"]
    msg = _sent_mime(payloads)
    assert msg.is_multipart()  # empty plain part + the html part
