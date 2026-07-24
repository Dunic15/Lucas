"""Gmail inbox brief — the first Gmail READ path (owner ask 2026-07-24:
"what's on my inbox?" had no answer at all, join or live).

Contract: headers only (from/subject/unread — never bodies), TTL-cached,
NEVER negative-cached, native OAuth first with the Pipedream Connect proxy as
fallback, and the deterministic capability answer keys on
session.gmail_brief_loaded. Key-free — every network edge is stubbed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import capabilities as cap  # noqa: E402
from app.integrations import google_client as gc  # noqa: E402


def _msgs():
    return [
        {"from": "Marco Rossi <m@acme.com>", "subject": "Q3 numbers",
         "unread": True},
        {"from": "dana@acme.com", "subject": "Invoice attached",
         "unread": False},
    ]


def setup_function(_fn):
    gc._inbox_brief_cache.clear()


# ── the distilled brief: headers only, honest counts ────────────────────
def test_brief_lines_are_header_only(monkeypatch):
    monkeypatch.setattr(gc, "list_inbox_messages",
                        lambda org, max_results=8: {"ok": True,
                                                    "messages": _msgs()})
    brief = gc.gmail_inbox_brief("org-x")
    assert "Marco Rossi — Q3 numbers [unread]" in brief
    assert "Invoice attached" in brief
    assert "1 unread" in brief
    assert "m@acme.com" not in brief  # display name shown, address dropped
    assert "body" not in brief.lower()


def test_brief_falls_back_to_pipedream(monkeypatch):
    monkeypatch.setattr(gc, "list_inbox_messages",
                        lambda org, max_results=8: {"ok": False,
                                                    "error": "HTTP 403"})
    from app import pipedream_executor as pe
    monkeypatch.setattr(pe, "read_gmail_inbox",
                        lambda org, max_results=8: {"ok": True,
                                                    "messages": _msgs()})
    brief = gc.gmail_inbox_brief("org-x")
    assert "Q3 numbers" in brief


def test_empty_read_is_never_negative_cached(monkeypatch):
    """The asana_client lesson: one transient empty read must not poison the
    TTL — the next join retries and gets the real inbox."""
    calls = {"n": 0}

    def _read(org, max_results=8):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "error": "transient"}
        return {"ok": True, "messages": _msgs()}

    monkeypatch.setattr(gc, "list_inbox_messages", _read)
    from app import pipedream_executor as pe
    monkeypatch.setattr(pe, "read_gmail_inbox",
                        lambda org, max_results=8: {"ok": False, "error": "off"})
    assert gc.gmail_inbox_brief("org-x") == ""
    assert "Q3 numbers" in gc.gmail_inbox_brief("org-x")  # retried, not cached


def test_good_brief_is_ttl_cached(monkeypatch):
    calls = {"n": 0}

    def _read(org, max_results=8):
        calls["n"] += 1
        return {"ok": True, "messages": _msgs()}

    monkeypatch.setattr(gc, "list_inbox_messages", _read)
    gc.gmail_inbox_brief("org-x")
    gc.gmail_inbox_brief("org-x")
    assert calls["n"] == 1  # second hit served from the TTL cache


# ── capability truth: the inbox brief drives the honest answer ──────────
class _Sess:
    def __init__(self, gmail=False, asana=False):
        self.asana_live = False
        self.asana_brief_loaded = asana
        self.gmail_brief_loaded = gmail
        self.tool_registry = {"native": [
            {"name": "gmail_send", "connected": True, "write": True,
             "verbs": "send email, reply, archive"},
            {"name": "asana_tasks", "connected": True, "write": True,
             "verbs": "create task"},
        ]}


def test_inbox_snapshot_answer_is_honest_both_ways(monkeypatch):
    from app.actions import executor
    monkeypatch.setattr(executor, "route_for_typed", lambda t, o: "native")
    # brief loaded → she says she has the inbox snapshot
    snap = cap.snapshot("petra", "o", _Sess(gmail=True))
    a = cap.answer("do you have a snapshot of my inbox?", snap)
    assert cap.is_capability_question("do you have a snapshot of my inbox?")
    assert "I have a snapshot of your Gmail inbox" in a
    # no brief → honest gap, phrased for the INBOX, not a "workspace"
    snap2 = cap.snapshot("petra", "o", _Sess(gmail=False))
    a2 = cap.answer("can you read my gmail?", snap2)
    assert "Gmail inbox loaded" in a2 or "no inbox snapshot" in \
        snap2["tools"]["gmail_send"]["unavailable_reason"]


def test_cache_recomputes_when_gmail_flag_flips(monkeypatch):
    from app.actions import executor
    monkeypatch.setattr(executor, "route_for_typed", lambda t, o: "native")
    s = _Sess(gmail=False)
    first = cap.cached_snapshot("petra", "o", s)
    assert first["tools"]["gmail_send"]["snapshot_available_in_meeting"] is False
    s.gmail_brief_loaded = True
    second = cap.cached_snapshot("petra", "o", s)
    assert second["tools"]["gmail_send"]["snapshot_available_in_meeting"] is True
