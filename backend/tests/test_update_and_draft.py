"""calendar.update_event + email.draft — the Cedric PA typed actions.

Covers the google_client wire behaviour (resolve-by-title, ambiguity fails
safe, drafts.create + scope self-heal), the engine producer (sanitize, stub
mapping, the draft-first downgrade), the executor registration/dispatch, and
the action-plane schemas. Key-free: httpx + the token seam are monkeypatched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.brain.engine as brain
from app import action_plane, executor, google_client
from app.config import settings


class _Resp:
    def __init__(self, code, payload):
        self.status_code = code
        self._payload = payload

    def json(self):
        return self._payload


def _tok(monkeypatch):
    monkeypatch.setattr(
        google_client, "_access_token",
        lambda principal, oauth=None, *, on_rotate=None, force_refresh=False:
        (("tok-fresh" if force_refresh else "tok-1"), ""),
    )


def _events(*summaries):
    return {"ok": True, "events": [
        {"id": f"ev{i}", "summary": s, "htmlLink": f"https://cal/{i}",
         "start": {"dateTime": "2026-08-03T15:00:00Z"}}
        for i, s in enumerate(summaries)
    ]}


# ── google_client.update_calendar_event ────────────────────────────────────

def test_update_resolves_single_match_and_patches(monkeypatch):
    _tok(monkeypatch)
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, **kw: _events("Client Meeting", "Other Sync"),
    )
    patches = []
    monkeypatch.setattr(google_client.httpx, "get",
                        lambda *a, **k: _Resp(200, {"id": "ev0"}))

    def fake_patch(url, params=None, headers=None, json=None, timeout=None):
        patches.append((url, params, json))
        return _Resp(200, {"id": "ev0", "htmlLink": "https://cal/0"})

    monkeypatch.setattr(google_client.httpx, "patch", fake_patch)
    r = google_client.update_calendar_event("org-1", {
        "title": "client meeting", "original_day": "2026-08-03",
        "start": "2026-08-03T16:00:00", "end": "2026-08-03T16:30:00",
    })
    assert r["ok"] is True and r["event_id"] == "ev0"
    url, params, body = patches[0]
    assert "ev0" in url
    assert params == {"sendUpdates": "all"}
    assert body["start"]["dateTime"] == "2026-08-03T16:00:00"


@pytest.mark.parametrize("titles,frag", [
    ((), "no event titled"),
    (("Client Meeting", "Client Meeting"), "2 events titled"),
])
def test_update_fails_safe_on_zero_or_many(monkeypatch, titles, frag):
    _tok(monkeypatch)
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, **kw: _events(*titles),
    )
    called = []
    monkeypatch.setattr(google_client.httpx, "patch",
                        lambda *a, **k: called.append(1) or _Resp(200, {}))
    r = google_client.update_calendar_event("org-1", {
        "title": "Client Meeting", "original_day": "2026-08-03",
        "start": "2026-08-03T16:00:00", "end": "2026-08-03T16:30:00",
    })
    assert r["ok"] is False and frag in r["error"]
    assert not called  # ambiguity/absence never reaches a PATCH


def test_update_with_explicit_event_id_skips_resolve(monkeypatch):
    _tok(monkeypatch)
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, **kw: (_ for _ in ()).throw(AssertionError("resolve ran")),
    )
    monkeypatch.setattr(google_client.httpx, "get",
                        lambda *a, **k: _Resp(200, {"id": "evX"}))
    monkeypatch.setattr(google_client.httpx, "patch",
                        lambda *a, **k: _Resp(200, {"id": "evX"}))
    r = google_client.update_calendar_event("org-1", {
        "event_id": "evX",
        "start": "2026-08-03T16:00:00", "end": "2026-08-03T16:30:00",
    })
    assert r["ok"] is True and r["event_id"] == "evX"


# ── google_client.create_gmail_draft ───────────────────────────────────────

def test_draft_posts_wrapped_message(monkeypatch):
    _tok(monkeypatch)
    posts = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append((url, json))
        return _Resp(200, {"id": "d1", "message": {"id": "m1", "threadId": "t1"}})

    monkeypatch.setattr(google_client.httpx, "post", fake_post)
    r = google_client.create_gmail_draft("org-1", {
        "to": "duccio@sffstudio.com", "subject": "Recap", "body": "Notes",
    })
    assert r == {"ok": True, "draft_id": "d1", "message_id": "m1",
                 "thread_id": "t1"}
    url, body = posts[0]
    assert url.endswith("/drafts")
    assert "raw" in body["message"]  # same MIME payload shape as a send


def test_draft_missing_scope_yields_reconnect_message(monkeypatch):
    _tok(monkeypatch)
    monkeypatch.setattr(google_client, "_drop_cached_token", lambda k: None)
    monkeypatch.setattr(google_client.httpx, "post", lambda *a, **k: _Resp(403, {}))
    r = google_client.create_gmail_draft("org-1", {
        "to": "a@b.com", "subject": "s", "body": "b",
    })
    assert r["ok"] is False and "reconnect google" in r["error"].lower()


# ── engine: sanitize + stub + draft-first ──────────────────────────────────

def _action(item, **kw):
    return dict({"item": item}, **kw)


def test_sanitize_update_requires_day_and_times():
    ok = brain._sanitize_typed(
        {"type": "calendar.update_event",
         "args": {"title": "Client Meeting", "original_day": "2026-08-03",
                  "start": "2026-08-03T16:00:00", "end": "2026-08-03T16:30:00"}},
        _action("Move the client meeting"),
    )
    assert ok and ok["type"] == "calendar.update_event"
    for broken in (
        {"title": "", "original_day": "2026-08-03",
         "start": "2026-08-03T16:00:00", "end": "2026-08-03T16:30:00"},
        {"title": "X", "original_day": "next Friday",
         "start": "2026-08-03T16:00:00", "end": "2026-08-03T16:30:00"},
        {"title": "X", "original_day": "2026-08-03",
         "start": "4pm", "end": "2026-08-03T16:30:00"},
    ):
        assert brain._sanitize_typed(
            {"type": "calendar.update_event", "args": broken}, _action("x")
        ) is None


def test_stub_maps_reschedule_intent():
    actions = [_action(
        "Reschedule the client meeting from 2026-08-03 to "
        "2026-08-04T15:00:00 until 2026-08-04T15:30:00"
    )]
    out = brain.type_actions(actions, provider="stub")
    typed = out[0].get("typed")
    assert typed and typed["type"] == "calendar.update_event"
    assert typed["args"]["original_day"] == "2026-08-03"
    assert typed["args"]["start"] == "2026-08-04T15:00:00"


def test_stub_maps_draft_intent_to_draft():
    actions = [_action("Draft an email to duccio@sffstudio.com about the recap")]
    out = brain.type_actions(actions, provider="stub")
    typed = out[0].get("typed")
    assert typed and typed["type"] == "email.draft"
    assert typed["args"]["to"] == ["duccio@sffstudio.com"]


def test_draft_first_downgrades_external_send(monkeypatch):
    monkeypatch.setattr(settings, "email_draft_first", True)
    actions = [_action("Send an email to duccio@sffstudio.com with the notes")]
    out = brain.type_actions(actions, provider="stub", owner_email="ananth@acme.com")
    assert out[0]["typed"]["type"] == "email.draft"


def test_draft_first_keeps_internal_send(monkeypatch):
    monkeypatch.setattr(settings, "email_draft_first", True)
    actions = [_action("Send an email to marco@acme.com with the notes")]
    out = brain.type_actions(actions, provider="stub", owner_email="ananth@acme.com")
    assert out[0]["typed"]["type"] == "email.send"


def test_draft_first_unknown_owner_drafts_everything(monkeypatch):
    monkeypatch.setattr(settings, "email_draft_first", True)
    actions = [_action("Send an email to marco@acme.com with the notes")]
    out = brain.type_actions(actions, provider="stub", owner_email="")
    assert out[0]["typed"]["type"] == "email.draft"


def test_draft_first_flag_off_is_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "email_draft_first", False)
    actions = [_action("Send an email to duccio@sffstudio.com with the notes")]
    out = brain.type_actions(actions, provider="stub", owner_email="ananth@acme.com")
    assert out[0]["typed"]["type"] == "email.send"


# ── executor + action plane registration ───────────────────────────────────

def test_from_typed_bridges_new_types():
    up = executor.from_typed({"type": "calendar.update_event",
                              "args": {"title": "X"}})
    assert up == {"type": "calendar.update_event", "event": {"title": "X"}}
    dr = executor.from_typed({"type": "email.draft", "args": {"to": ["a@b.com"]}})
    assert dr == {"type": "email.draft", "message": {"to": ["a@b.com"]}}
    assert executor.capability_family("calendar.update_event") == "google"
    assert executor.capability_family("email.draft") == "google"


def test_execute_approved_dispatches_new_types(monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    calls = []
    monkeypatch.setattr(
        executor.google_client, "update_calendar_event",
        lambda org, ev: calls.append(("update", ev)) or
        {"ok": True, "event_url": "https://cal/0", "event_id": "ev0"},
    )
    monkeypatch.setattr(
        executor.google_client, "create_gmail_draft",
        lambda org, msg: calls.append(("draft", msg)) or
        {"ok": True, "draft_id": "d1"},
    )
    monkeypatch.setattr(executor.ledger, "set_action_status",
                        lambda *a, **k: None)
    r1 = executor.execute_approved("org-1", "a1", {
        "type": "calendar.update_event", "event": {"event_id": "ev0"},
    })
    r2 = executor.execute_approved("org-1", "a2", {
        "type": "email.draft", "message": {"to": ["a@b.com"], "subject": "s"},
    })
    assert r1["ok"] and r2["ok"]
    assert [c[0] for c in calls] == ["update", "draft"]


def test_action_plane_schemas_and_risk():
    up = {"type": "calendar.update_event", "args": {"title": "X"}}
    missing = [f["name"] for f in action_plane.params_schema(up) if f["required"]]
    assert set(missing) == {"title", "original_day", "start", "end"}
    assert action_plane.missing_params(up)  # incomplete → needs_details gate
    assert action_plane.risk_for(up) == "low"
    dr = {"type": "email.draft",
          "args": {"to": ["a@b.com"], "subject": "s", "body": "b"}}
    assert action_plane.missing_params(dr) == []
    assert action_plane.risk_for(dr) == "low"
