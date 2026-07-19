"""The dashboard chat channel — Cedric's in-dashboard approval surface.

Three seams, all key-free (sqlite in tmp_path, Cedric faked at the callback
layer):
  1. POST /org/chat        — Cedric posts text / action cards (machine gate,
                             org-scoped like every /org door).
  2. GET/POST /dashboard/chat — the human side: same four-worlds gate as
                             /dashboard/summary (cookie user / per-org bearer /
                             global bearer / key-free demo) + same-origin POST;
                             GET enriches referenced action cards with live
                             typed/execution state; POST stores then relays
                             chat.message over the signed events door.
  3. Decisions NEVER live in chat — cards reference action_id and the UI hits
     the existing canonical approve/reject doors (covered by
     test_dashboard_approve.py); here we assert the enrichment reflects the
     ledger truth those doors write.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, ledger, store
from app.cedric import callback as cedric_callback
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    monkeypatch.setattr(settings, "native_executor", False)
    return TestClient(main_module.app)


def _login(client: TestClient, email: str = "owner@x.com") -> dict:
    user = store.upsert_user(email)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def _seed_action(org: str, action_id: str) -> None:
    action = {"item": "Email the recap", "owner": "Ben", "action_id": action_id,
              "typed": {"type": "email.send", "args": {"to": ["m@x.com"]}}}
    store.save_artifact(
        f"bot_{action_id}",
        {
            "summary": "Kickoff.",
            "actions": [action],
            "org_id": org,
            "avatar_id": "laura",
            "transcript": "PII must never leak",
        },
        org_id=org,
    )


# ───────────────────────── store layer ─────────────────────────
def test_chat_rows_are_org_scoped(client):
    store.add_chat_message("org_a", "user", body="hello from a")
    store.add_chat_message("org_b", "cedric", body="hello from b")
    a = store.list_chat_messages("org_a")
    assert [m["body"] for m in a] == ["hello from a"]
    assert store.list_chat_messages("org_b")[0]["sender"] == "cedric"
    # cursor: nothing after the last id
    assert store.list_chat_messages("org_a", after_id=a[-1]["id"]) == []


def test_chat_rejects_junk(client):
    assert store.add_chat_message("", "user", body="x") is None
    assert store.add_chat_message("org_a", "attacker", body="x") is None
    assert store.add_chat_message("org_a", "user", body="   ") is None


# ───────────────────── inbound: Cedric → /org/chat ─────────────────────
def test_org_chat_posts_message_to_demo_org(client):
    resp = client.post("/org/chat", json={"message": {"text": "Ready when you are."}})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    msgs = store.list_chat_messages(settings.demo_org_id)
    assert msgs[-1]["body"] == "Ready when you are."
    assert msgs[-1]["sender"] == "cedric"


def test_org_chat_scopes_by_per_org_bearer(client):
    raw = store.mint_org_token("org_acme", "test")
    resp = client.post(
        "/org/chat",
        json={"message": {"text": "acme only"}},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200
    assert store.list_chat_messages("org_acme")[-1]["body"] == "acme only"
    assert store.list_chat_messages(settings.demo_org_id) == []


def test_org_chat_action_card_shape(client):
    bad = client.post("/org/chat", json={"action_card": {"item": "no id"}})
    assert bad.status_code == 400
    both = client.post(
        "/org/chat",
        json={"message": {"text": "x"}, "action_card": {"action_id": "a", "item": "y"}},
    )
    assert both.status_code == 400
    ok = client.post(
        "/org/chat",
        json={"action_card": {"action_id": "act123", "item": "Send the recap",
                              "owner": "Dana", "due": "Friday",
                              "note": "One thing needs your sign-off:"}},
    )
    assert ok.status_code == 200
    row = store.list_chat_messages(settings.demo_org_id)[-1]
    assert row["kind"] == "action_card" and row["action_id"] == "act123"
    assert row["payload"] == {"item": "Send the recap", "owner": "Dana", "due": "Friday"}


def test_org_chat_requires_auth_when_token_set(client, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "sekrit")
    assert client.post("/org/chat", json={"message": {"text": "x"}}).status_code == 401


# ──────────────────── human side: /dashboard/chat ────────────────────
def test_dashboard_chat_requires_login_when_auth_enabled(client, monkeypatch):
    """A login-gated deployment still gates chat for anonymous browsers."""
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "sec")
    assert client.get("/dashboard/chat").status_code == 401
    assert client.post("/dashboard/chat", json={"text": "hi"}).status_code == 401


def test_dashboard_chat_keyfree_maps_to_demo_org(client, monkeypatch):
    """Key-free world: chat serves the demo org like every other dashboard
    surface (a blanket 401 here left the whole tab dead on 'Loading…' —
    live repro 2026-07-19). Same four-worlds gate as /dashboard/summary."""
    monkeypatch.setattr(cedric_callback, "send_action_event", lambda *a: False)
    listing = client.get("/dashboard/chat")
    assert listing.status_code == 200
    assert listing.json()["messages"] == []
    resp = client.post("/dashboard/chat", json={"text": "hi Cedric"})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = store.list_chat_messages(settings.demo_org_id)[-1]
    assert row["body"] == "hi Cedric" and row["sender"] == "user"


def test_dashboard_chat_send_stores_and_relays(client, monkeypatch):
    user = _login(client)
    sent: list[tuple] = []
    monkeypatch.setattr(
        cedric_callback, "send_action_event",
        lambda org, event, fields: sent.append((org, event, fields)) or True,
    )
    resp = client.post(
        "/dashboard/chat", json={"text": "Cedric, chase the DPA please"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["delivered"] is True

    org, event, fields = sent[0]
    assert org == user["org_id"] and event == "chat.message"
    assert fields["text"] == "Cedric, chase the DPA please"
    assert fields["message_id"] == body["id"]

    msgs = store.list_chat_messages(user["org_id"])
    assert msgs[-1]["sender"] == "user" and "DPA" in msgs[-1]["body"]


def test_dashboard_chat_send_reports_failed_relay(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(cedric_callback, "send_action_event", lambda *a: False)
    resp = client.post(
        "/dashboard/chat", json={"text": "anyone home?"},
    )
    assert resp.status_code == 200
    assert resp.json()["delivered"] is False  # stored anyway — honesty, not loss


def test_dashboard_chat_list_cursor_and_org_isolation(client):
    user = _login(client)
    store.add_chat_message(user["org_id"], "cedric", body="mine")
    store.add_chat_message("org_other", "cedric", body="not mine")
    d = client.get("/dashboard/chat").json()
    assert [m["body"] for m in d["messages"]] == ["mine"]
    last = d["messages"][-1]["id"]
    assert client.get(f"/dashboard/chat?after={last}").json()["messages"] == []


def test_dashboard_chat_enriches_action_cards(client):
    user = _login(client)
    org = user["org_id"]
    _seed_action(org, "act_lv")
    store.add_chat_message(
        org, "cedric", kind="action_card", action_id="act_lv",
        body="Needs your sign-off:", payload={"item": "Email the recap"},
    )
    d = client.get("/dashboard/chat").json()
    card = d["actions"]["act_lv"]
    assert card["known"] is True and card["typed"] is True
    assert card["execution"] is None  # no decision yet → UI renders the buttons

    # A decision lands in the ledger (what the canonical doors write) → the
    # SAME poll now carries the receipt, so the card flips to a pill.
    ledger.set_action_status("act_lv", "done", "https://mail.google.com/x", org_id=org)
    d = client.get("/dashboard/chat").json()
    ex = d["actions"]["act_lv"]["execution"]
    assert ex["status"] == "done" and "mail.google" in ex["detail"]


def test_dashboard_chat_flags_unknown_card(client):
    user = _login(client)
    store.add_chat_message(
        user["org_id"], "cedric", kind="action_card", action_id="ghost",
        payload={"item": "??"},
    )
    d = client.get("/dashboard/chat").json()
    assert d["actions"]["ghost"]["known"] is False


def test_dashboard_chat_never_leaks_transcripts(client):
    user = _login(client)
    _seed_action(user["org_id"], "act_p")
    store.add_chat_message(
        user["org_id"], "cedric", kind="action_card", action_id="act_p",
        payload={"item": "Email the recap"},
    )
    raw = client.get("/dashboard/chat").text
    assert "PII must never leak" not in raw
