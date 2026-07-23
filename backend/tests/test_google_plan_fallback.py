"""Native-first Google execution with Pipedream Connect fallback.

Synthetic/key-free: every vendor and credential seam is monkeypatched. These
tests pin the dual-plane cutover without making a Google or Pipedream request.
"""
from __future__ import annotations

from app import native_runtime, store
from app.actions import executor
from app.config import settings


_EMAIL = {
    "type": "email.send",
    "message": {
        "to": ["person@example.com"],
        "subject": "Recap",
        "body": "Synthetic notes",
    },
}
_CALENDAR = {
    "type": "calendar.create_event",
    "event": {
        "title": "Synthetic sync",
        "start": "2026-07-23T15:00:00+02:00",
        "end": "2026-07-23T15:30:00+02:00",
    },
}


def _on(monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)


def test_dual_plane_google_types_stamp_native_even_when_pd_connected(monkeypatch):
    """Capture-time probes never choose the final Google plan."""
    _on(monkeypatch)
    from app import pipedream_executor

    monkeypatch.setattr(
        pipedream_executor, "app_connected", lambda org, app: True
    )
    assert executor.route_for_typed(
        {"type": "email.send", "args": {}}, "org-x"
    ) == "native"
    assert executor.route_for_typed(
        {"type": "calendar.create_event", "args": {}}, "org-x"
    ) == "native"


def test_native_ok_never_calls_pipedream(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(
        store, "get_org_oauth", lambda *a, **k: {"refresh_token": "synthetic"}
    )
    calls = []
    monkeypatch.setattr(
        native_runtime,
        "execute",
        lambda org, action: calls.append(("native", org, action)) or {
            "ok": True,
            "kind": "email",
            "message_id": "m-native",
            "ref": "m-native",
        },
    )
    monkeypatch.setattr(
        executor.google_client, "verify_gmail_message", lambda *a: True
    )
    monkeypatch.setattr(
        executor,
        "_pipedream_google_connected",
        lambda *a: (_ for _ in ()).throw(
            AssertionError("Pipedream probed after native success")
        ),
    )

    out = executor.execute_approved("org-x", "", _EMAIL)
    assert out["ok"] is True
    assert calls and calls[0][0] == "native"


def test_native_absent_uses_matching_pipedream_plan(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(store, "get_org_oauth", lambda *a, **k: None)
    monkeypatch.setattr(
        executor, "_pipedream_google_connected", lambda org, typ: typ == "email.send"
    )
    monkeypatch.setattr(
        native_runtime,
        "execute",
        lambda *a: (_ for _ in ()).throw(
            AssertionError("native called without org OAuth")
        ),
    )
    seen = {}
    monkeypatch.setattr(
        executor,
        "_execute_pipedream_fallback",
        lambda org, aid, action: seen.update(
            org=org, action=action
        ) or {
            "ok": True,
            "route": "pipedream",
            "kind": "email · verified",
            "verified": True,
        },
    )

    out = executor.execute_approved("org-x", "a1", _EMAIL)
    assert out["ok"] and out["route"] == "pipedream"
    assert out["verified"] is True and "verified" in out["kind"]
    assert seen["action"]["type"] == "email.send"


def test_both_plans_absent_keeps_native_reconnect_hint(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(store, "get_org_oauth", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_pipedream_google_connected", lambda *a: False)
    monkeypatch.setattr(
        native_runtime,
        "execute",
        lambda *a: {
            "ok": False,
            "kind": "Google Calendar",
            "error": "Google is not connected for this org",
        },
    )

    out = executor.execute_approved("org-x", "", _CALENDAR)
    assert not out["ok"]
    assert out["error"] == "Google is not connected for this org"


def test_native_auth_failure_can_fall_back_before_write(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(
        store, "get_org_oauth", lambda *a, **k: {"refresh_token": "synthetic"}
    )
    monkeypatch.setattr(
        native_runtime,
        "execute",
        lambda *a: {
            "ok": False,
            "kind": "Gmail",
            "error": "token refresh rejected (HTTP 401)",
        },
    )
    monkeypatch.setattr(executor, "_pipedream_google_connected", lambda *a: True)
    monkeypatch.setattr(
        executor,
        "_execute_pipedream_fallback",
        lambda *a: {"ok": True, "route": "pipedream", "verified": True},
    )

    out = executor.execute_approved("org-x", "a2", _EMAIL)
    assert out == {"ok": True, "route": "pipedream", "verified": True}


def test_post_write_vendor_failure_never_cross_plane_retries(monkeypatch):
    """An ambiguous Calendar 5xx may have landed; do not duplicate it via PD."""
    _on(monkeypatch)
    monkeypatch.setattr(
        store, "get_org_oauth", lambda *a, **k: {"refresh_token": "synthetic"}
    )
    monkeypatch.setattr(
        native_runtime,
        "execute",
        lambda *a: {
            "ok": False,
            "kind": "Google Calendar",
            "error": "calendar insert failed (HTTP 500)",
        },
    )
    monkeypatch.setattr(executor, "_pipedream_google_connected", lambda *a: True)
    monkeypatch.setattr(
        executor,
        "_execute_pipedream_fallback",
        lambda *a: (_ for _ in ()).throw(
            AssertionError("unsafe cross-plane retry")
        ),
    )

    out = executor.execute_approved("org-x", "", _CALENDAR)
    assert not out["ok"] and "HTTP 500" in out["error"]

