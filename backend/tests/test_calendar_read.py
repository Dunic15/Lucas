"""Calendar sight: the avatar knows the owner's upcoming Google Calendar.

Same contract as the Drive folder brief: assembled once at session start,
best-effort ("" when no Google is connected — the join proceeds), bounded
(it rides the live prompt), cached per org, content never logged. The
upcoming_meetings brain tool reads the session snapshot — zero network live.
"""
from __future__ import annotations

from types import SimpleNamespace

from app import google_client, tools


def _items():
    return [
        {
            "summary": "Weekly Planning",
            "start": {"dateTime": "2026-07-20T14:00:00+02:00"},
            "end": {"dateTime": "2026-07-20T14:30:00+02:00"},
            "attendees": [
                {"email": "owner@acme.com", "self": True},
                {"displayName": "Marco Rossi", "email": "marco@acme.com"},
                {"email": "priya@acme.com"},
            ],
        },
        {
            "summary": "Offsite",
            "start": {"date": "2026-07-22"},
            "end": {"date": "2026-07-23"},
        },
    ]


def test_brief_formats_time_title_guests(monkeypatch):
    google_client._brief_cache.clear()
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, max_results=8: {"ok": True, "events": _items()},
    )
    brief = google_client.calendar_brief("org-a")
    assert "Weekly Planning" in brief
    assert "14:00–14:30" in brief
    assert "Marco Rossi" in brief and "priya" in brief
    assert "owner" not in brief  # self is excluded
    assert "2026-07-22 (all day)" in brief


def test_brief_empty_when_not_connected(monkeypatch):
    google_client._brief_cache.clear()
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, max_results=8: {"ok": False, "error": "no oauth"},
    )
    assert google_client.calendar_brief("org-b") == ""


def test_brief_cached_per_org(monkeypatch):
    google_client._brief_cache.clear()
    calls = []

    def _fake(org, max_results=8):
        calls.append(org)
        return {"ok": True, "events": _items()}

    monkeypatch.setattr(google_client, "list_calendar_events", _fake)
    google_client.calendar_brief("org-c")
    google_client.calendar_brief("org-c")
    assert calls == ["org-c"]  # second hit served from cache


def test_brief_bounded(monkeypatch):
    google_client._brief_cache.clear()
    many = [
        {
            "summary": f"Very long meeting title that goes on and on {i}",
            "start": {"dateTime": "2026-07-20T14:00:00+02:00"},
        }
        for i in range(50)
    ]
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, max_results=8: {"ok": True, "events": many},
    )
    brief = google_client.calendar_brief("org-d")
    assert len(brief) <= google_client._BRIEF_MAX_CHARS
    assert len(brief.splitlines()) <= google_client._BRIEF_MAX_EVENTS


def test_brief_blank_org_is_empty():
    assert google_client.calendar_brief("") == ""


def test_upcoming_meetings_tool_reads_snapshot():
    session = SimpleNamespace(calendar_brief="- Mon 20 Jul 14:00 — Weekly Planning")
    out = tools.upcoming_meetings(session=session)
    assert "Weekly Planning" in out


def test_upcoming_meetings_tool_degrades_helpfully():
    assert "connect Google" in tools.upcoming_meetings(session=None)
    assert "connect Google" in tools.upcoming_meetings(
        session=SimpleNamespace(calendar_brief="")
    )


def test_tool_registered_for_brain_and_session():
    assert "upcoming_meetings" in tools._DISPATCH
    assert "upcoming_meetings" in tools._SESSION_TOOLS
    names = {
        t["function"]["name"]
        for t in tools.TOOL_SPECS
        if t.get("type") == "function"
    }
    assert "upcoming_meetings" in names  # the brain can actually see it


# ── all-calendars fan-out: selected calendars merge into one upcoming view ──


def _resp(json_data, status=200):
    return SimpleNamespace(status_code=status, json=lambda: json_data)


def test_list_events_merges_selected_calendars(monkeypatch):
    monkeypatch.setattr(
        google_client, "_access_token",
        lambda key, oauth=None, on_rotate=None, force_refresh=False: ("tok", ""),
    )

    def fake_get(url, params=None, headers=None, timeout=None):
        if "users/me/calendarList" in url:
            return _resp({"items": [
                {"id": "duccio@example.com", "primary": True, "selected": True},
                {"id": "team@group.calendar.google.com", "selected": True},
                {"id": "ignored@cal", "selected": False},
            ]})
        if "duccio%40example.com" in url:
            return _resp({"items": [
                {"id": "a1", "iCalUID": "uid-a", "summary": "Later",
                 "start": {"dateTime": "2026-07-21T10:00:00Z"}},
                {"id": "dup1", "iCalUID": "uid-shared", "summary": "Shared copy",
                 "start": {"dateTime": "2026-07-22T10:00:00Z"}},
            ]})
        if "team%40group.calendar.google.com" in url:
            return _resp({"items": [
                {"id": "b1", "iCalUID": "uid-b", "summary": "Sooner",
                 "start": {"dateTime": "2026-07-20T09:00:00Z"}},
                {"id": "dup2", "iCalUID": "uid-shared", "summary": "Shared copy",
                 "start": {"dateTime": "2026-07-22T10:00:00Z"}},
            ]})
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(google_client.httpx, "get", fake_get)
    out = google_client.list_calendar_events("org-x")
    assert out["ok"] is True
    titles = [e["summary"] for e in out["events"]]
    assert titles[0] == "Sooner"  # merged + sorted across calendars
    assert titles.count("Shared copy") == 1  # deduped on iCalUID
    assert "ignored" not in str(out["events"])  # unselected calendar untouched


def test_list_events_falls_back_to_primary_when_calendarlist_fails(monkeypatch):
    monkeypatch.setattr(
        google_client, "_access_token",
        lambda key, oauth=None, on_rotate=None, force_refresh=False: ("tok", ""),
    )
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if "users/me/calendarList" in url:
            return _resp({}, status=500)
        assert "calendars/primary/events" in url
        return _resp({"items": _items()})

    monkeypatch.setattr(google_client.httpx, "get", fake_get)
    out = google_client.list_calendar_events("org-y")
    assert out["ok"] is True and len(out["events"]) == 2
    assert sum("calendars/primary/events" in u for u in calls) == 1


def test_list_events_error_only_when_nothing_readable(monkeypatch):
    monkeypatch.setattr(
        google_client, "_access_token",
        lambda key, oauth=None, on_rotate=None, force_refresh=False: ("tok", ""),
    )

    def fake_get(url, params=None, headers=None, timeout=None):
        if "users/me/calendarList" in url:
            return _resp({"items": [{"id": "x@cal", "selected": True, "primary": True}]})
        return _resp({}, status=403)

    monkeypatch.setattr(google_client.httpx, "get", fake_get)
    out = google_client.list_calendar_events("org-z")
    assert out["ok"] is False and "403" in out["error"]


# ── stale-token self-heal after a scope-adding reconnect (the "reconnected
#    but still don't see them" bug) ────────────────────────────────────────

def test_calendarlist_403_remints_and_reveals_all_calendars(monkeypatch):
    """A reconnect that ADDS calendar.readonly leaves the OLD access token in
    the cache (old scopes) → calendarList 403 → primary-only. The fan-out must
    re-mint ONCE from the new refresh token and retry, revealing the shared
    calendar's events (e.g. a 'Weekly Planning' the user couldn't see)."""
    google_client._reset_token_cache()
    tokens = iter(["stale-old-scope", "fresh-new-scope"])
    minted = []

    def fake_token(key, oauth=None, on_rotate=None, force_refresh=False):
        t = next(tokens)
        minted.append((t, force_refresh))
        return t, ""

    monkeypatch.setattr(google_client, "_access_token", fake_token)

    def fake_get(url, params=None, headers=None, timeout=None):
        bearer = (headers or {}).get("Authorization", "")
        if "users/me/calendarList" in url:
            # the stale token 403s; only the re-minted (fresh) token succeeds
            if "fresh-new-scope" not in bearer:
                return _resp({}, status=403)
            return _resp({"items": [
                {"id": "primary", "primary": True, "selected": True},
                {"id": "sff@group.calendar.google.com", "selected": True},
            ]})
        if "sff%40group.calendar.google.com" in url:
            return _resp({"items": [
                {"id": "wp", "iCalUID": "uid-wp", "summary": "Weekly Planning",
                 "start": {"dateTime": "2026-07-20T08:45:00Z"}},
            ]})
        return _resp({"items": [
            {"id": "p1", "iCalUID": "uid-p", "summary": "Primary standup",
             "start": {"dateTime": "2026-07-20T09:30:00Z"}},
        ]})

    monkeypatch.setattr(google_client.httpx, "get", fake_get)
    out = google_client.list_calendar_events("org-stale")
    assert out["ok"] is True
    titles = [e["summary"] for e in out["events"]]
    assert "Weekly Planning" in titles  # the shared-calendar event now shows
    # exactly one re-mint, and it was the force_refresh one
    assert minted == [("stale-old-scope", False), ("fresh-new-scope", True)]


def test_calendarlist_403_no_retry_loop_when_grant_truly_lacks_scope(monkeypatch):
    """If the re-mint returns the SAME token (the grant genuinely lacks the
    scope — user never granted it), do NOT loop: re-mint at most once, then
    fall back to primary-only. Guards Google's token endpoint from hammering."""
    google_client._reset_token_cache()
    monkeypatch.setattr(
        google_client, "_access_token",
        lambda key, oauth=None, on_rotate=None, force_refresh=False: ("same-tok", ""),
    )
    calls = {"calendarList": 0, "primary": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        if "users/me/calendarList" in url:
            calls["calendarList"] += 1
            return _resp({}, status=403)
        calls["primary"] += 1
        return _resp({"items": [
            {"id": "p1", "iCalUID": "uid-p", "summary": "Only primary",
             "start": {"dateTime": "2026-07-20T09:30:00Z"}},
        ]})

    monkeypatch.setattr(google_client.httpx, "get", fake_get)
    out = google_client.list_calendar_events("org-noscope")
    assert out["ok"] is True
    assert [e["summary"] for e in out["events"]] == ["Only primary"]
    assert calls["calendarList"] == 1  # tried once, no retry (same token)
    assert calls["primary"] == 1       # degraded to primary-only


def test_force_refresh_bypasses_cache(monkeypatch):
    """A cached, still-valid token is normally reused; force_refresh must skip
    it and re-mint (what the reconnect + 403 paths rely on)."""
    import time as _t
    google_client._reset_token_cache()
    monkeypatch.setattr(google_client.settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(google_client.settings, "google_calendar_client_secret", "csec")
    with google_client._TOKEN_LOCK:
        google_client._TOKEN_CACHE["p1"] = ("cached-tok", _t.time() + 9999)

    # normal read reuses the cache (no HTTP)
    tok, err = google_client._access_token("p1", {"refresh_token": "rt"})
    assert (tok, err) == ("cached-tok", "")

    def fake_post(url, data=None, timeout=None):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": "reminted", "expires_in": 3600},
        )

    monkeypatch.setattr(google_client.httpx, "post", fake_post)
    tok2, err2 = google_client._access_token(
        "p1", {"refresh_token": "rt"}, force_refresh=True
    )
    assert (tok2, err2) == ("reminted", "")


def test_force_refresh_persists_rotated_refresh_token(monkeypatch):
    """Rotation must survive the 403 self-heal: a force_refresh mint that
    returns a NEW refresh token persists it (org path -> set_org_oauth) and
    updates the in-memory dict so a later use redeems the rotated token."""
    google_client._reset_token_cache()
    monkeypatch.setattr(google_client.settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(google_client.settings, "google_calendar_client_secret", "csec")

    saved = {}
    monkeypatch.setattr(
        google_client.store, "get_org_oauth",
        lambda p, provider="google": {"refresh_token": "rt-old", "email": "o@x", "scopes": "s"},
    )
    monkeypatch.setattr(
        google_client.store, "set_org_oauth",
        lambda org, rt, provider="google", email="", scopes="": saved.update(org=org, rt=rt) or True,
    )

    def fake_post(url, data=None, timeout=None):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": "acc", "expires_in": 3600,
                          "refresh_token": "rt-new"},
        )

    monkeypatch.setattr(google_client.httpx, "post", fake_post)
    tok, err = google_client._access_token("org-rot", None, force_refresh=True)
    assert (tok, err) == ("acc", "")
    assert saved == {"org": "org-rot", "rt": "rt-new"}  # rotation persisted


def test_per_user_principal_self_heals_on_403(monkeypatch):
    """The bug is on the per-user dashboard view (principal 'user:<uid>').
    Drive the self-heal through that exact key + a supplied oauth dict."""
    google_client._reset_token_cache()
    tokens = iter(["stale", "fresh"])
    monkeypatch.setattr(
        google_client, "_access_token",
        lambda key, oauth=None, on_rotate=None, force_refresh=False: (next(tokens), ""),
    )

    def fake_get(url, params=None, headers=None, timeout=None):
        bearer = (headers or {}).get("Authorization", "")
        if "users/me/calendarList" in url:
            if "fresh" not in bearer:
                return _resp({}, status=403)
            return _resp({"items": [
                {"id": "primary", "primary": True, "selected": True},
                {"id": "team@g", "selected": True},
            ]})
        if "team%40g" in url:
            return _resp({"items": [{"id": "t", "iCalUID": "u-t", "summary": "Team event",
                                     "start": {"dateTime": "2026-07-20T08:00:00Z"}}]})
        return _resp({"items": []})

    monkeypatch.setattr(google_client.httpx, "get", fake_get)
    out = google_client.list_calendar_events(
        "org-x", oauth={"refresh_token": "rt"}, principal="user:abc"
    )
    assert out["ok"] is True
    assert "Team event" in [e["summary"] for e in out["events"]]


def test_surviving_403_is_negative_cached_then_reconnect_clears(monkeypatch):
    """A grant that truly lacks the scope must not re-pay the fan-out on every
    read: the first surviving 403 blocks calendarList for the TTL; a reconnect
    (_drop_cached_token) clears the block so the next read re-checks."""
    google_client._reset_token_cache()
    monkeypatch.setattr(
        google_client, "_access_token",
        lambda key, oauth=None, on_rotate=None, force_refresh=False: ("same", ""),
    )
    hits = {"calendarList": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        if "users/me/calendarList" in url:
            hits["calendarList"] += 1
            return _resp({}, status=403)
        return _resp({"items": [{"id": "p", "iCalUID": "u-p", "summary": "Primary",
                                 "start": {"dateTime": "2026-07-20T08:00:00Z"}}]})

    monkeypatch.setattr(google_client.httpx, "get", fake_get)

    google_client.list_calendar_events("org-block")   # 1st: tries + blocks
    google_client.list_calendar_events("org-block")   # 2nd: skipped by block
    assert hits["calendarList"] == 1                  # not re-paid

    google_client._drop_cached_token("org-block")     # a reconnect
    google_client.list_calendar_events("org-block")   # re-checks
    assert hits["calendarList"] == 2
