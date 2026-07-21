"""End-to-end: native calendar + email execute for ALL avatars, on approval,
INDEPENDENT of Slack/Cedric.

Pins the owner decision (2026-07-16): booking and email run natively in every
agent, separate from the Slack path; only routed to Cedric on an explicit ask
or when NATIVE_EXECUTOR is turned off. Google is mocked; key-free.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import executor, store  # noqa: E402
from app.config import settings  # noqa: E402


def _fresh_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()


class _Resp:
    def __init__(self, code, p):
        self.status_code, self._p = code, p

    def json(self):
        return self._p


def _mock_google(monkeypatch):
    from app import google_client

    monkeypatch.setattr(settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csec")
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        if url.endswith("/token"):
            return _Resp(200, {"access_token": "at"})
        if "calendar" in url:
            return _Resp(200, {"id": "ev1", "htmlLink": "https://cal/ev1",
                               "hangoutLink": "https://meet.google.com/x-y-z"})
        if "gmail" in url:
            return _Resp(200, {"id": "m1", "threadId": "t1"})
        return _Resp(404, {})

    monkeypatch.setattr(google_client.httpx, "post", fake_post)
    return calls


# ── the flag is now ON by default (owner decision) ──────────────────────────

def test_native_execution_on_by_default():
    assert executor.enabled() is True
    assert settings.execution_mode == "native"


# ── calendar + email actually execute natively, no Cedric ───────────────────

def test_calendar_executes_natively(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(settings, "session_secret", "sek")
    store.set_org_oauth("org-a", "rt")
    calls = _mock_google(monkeypatch)

    r = executor.execute_approved("org-a", "act-cal", {
        "type": "calendar.create_event",
        "event": {"title": "Follow-up", "start": "2026-08-01T10:00:00",
                  "end": "2026-08-01T10:30:00", "attendees": ["a@b.com"]},
    })
    assert r["ok"] is True
    assert any("calendar" in u for u in calls)          # hit Google Calendar
    assert not any("meet-cedric" in u or "cedric" in u for u in calls)  # NOT Slack


def test_email_executes_natively(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(settings, "session_secret", "sek")
    store.set_org_oauth("org-a", "rt")
    calls = _mock_google(monkeypatch)

    r = executor.execute_approved("org-a", "act-mail", {
        "type": "email.send",
        "message": {"to": "a@b.com", "subject": "Recap", "body": "Notes"},
    })
    assert r["ok"] is True and r.get("message_id") == "m1"
    assert any("gmail" in u for u in calls)


def test_executor_never_imports_cedric():
    """Native execution must be independent of the Slack/Cedric broker: no
    MODULE-LEVEL cedric import, so the executor loads and executes natively
    with cedric absent. (The approve-door reconciliation added a lazy,
    try-guarded action.status EVENT mirror to the Cedric surface; that
    reports provenance, it does not route execution, and it may no-op.)"""
    import app.executor as ex
    src = Path(ex.__file__).read_text()
    assert not hasattr(ex, "cedric")           # not imported at module level
    top_level_imports = [
        ln for ln in src.splitlines()
        if ln.startswith(("import ", "from "))  # column 0 = module level
    ]
    assert not any("cedric" in ln for ln in top_level_imports)
    # any lazy cedric use must be inside a try (best-effort, never load-bearing)
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if "from .cedric" in ln and ln.startswith(" "):
            window = "\n".join(lines[max(0, i - 6):i])
            assert "try:" in window, "lazy cedric import must be try-guarded"
    # it only reaches for the native vendor clients + the ledger
    assert "google_client" in src and "asana_client" in src and "ledger" in src


# ── EVERY avatar has Google (calendar+email) natively by default ────────────

def test_all_avatars_get_google_native_by_default(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    for aid in ("laura", "cedric", "petra"):
        # baseline: when the org has connected Google, the per-avatar `google`
        # toggle defaults ON for every avatar (calendar + email families).
        assert store.capability_enabled(aid, "google", connected=True) is True
        assert executor.capability_family("calendar.create_event") == "google"
        assert executor.capability_family("email.send") == "google"


def test_google_toggle_off_blocks_native_for_that_avatar(monkeypatch, tmp_path):
    """Not all agents must have it: an explicit per-avatar OFF is honored (the
    approve endpoint reads this and skips execution, capability_blocked)."""
    _fresh_store(monkeypatch, tmp_path)
    store.set_avatar_capability("laura", "google", False)
    assert store.capability_enabled("laura", "google", connected=True) is False
    # other avatars are unaffected
    assert store.capability_enabled("cedric", "google", connected=True) is True


# ── the flag off ⇒ nothing executes (byte-identical to the Cedric path) ──────

def test_flag_off_executes_nothing(monkeypatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "native_executor", False)
    r = executor.execute_approved("org-a", "act", {
        "type": "email.send", "message": {"to": "a@b.com", "subject": "x"},
    })
    assert r == {"ok": False, "skipped": "native_executor off"}
