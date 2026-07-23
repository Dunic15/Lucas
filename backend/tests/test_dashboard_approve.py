"""POST /dashboard/actions/{id}/approve — the native approve→execute loop.

Key-free: sqlite in tmp_path, Google mocked at executor.google_client, and the
NATIVE_EXECUTOR flag toggled per test. Asserts the two invariants that matter:
  1. auth + org scoping — a logged-in owner only, and only for their own org.
  2. the executor runs ONLY when the flag is on AND the action is typed; with
     the flag off (today's default) approve marks the row and executes nothing.
The receipt (event link / message id) lands in the same ledger provenance
channel the dashboard reads.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, executor, ledger, store
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)  # shares the sqlite file — needs its tables too
    monkeypatch.setattr(settings, "native_executor", False)
    return TestClient(main_module.app)


def _login(client: TestClient, email: str = "owner@x.com") -> dict:
    user = store.upsert_user(email)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


_EMAIL_TYPED = {
    "type": "email.send",
    "args": {"to": ["marco@acme.com"], "subject": "Recap", "body": "Notes"},
}


def _seed_action(org: str, action_id: str, typed: dict | None = None) -> None:
    action = {"item": "Email the recap to marco@acme.com", "owner": "Ben",
              "action_id": action_id}
    if typed is not None:
        action["typed"] = typed
    store.save_artifact(
        f"bot_{action_id}",
        {
            "summary": "Kickoff.",
            "actions": [action],
            "checklist": [action],
            "org_id": org,
            "avatar_id": "laura",
            "meeting_url": "https://meet.google.com/appr-test",
            "transcript": "PII must never leak",
        },
        org_id=org,
    )


def _mock_send(monkeypatch, result: dict, sink: list | None = None):
    def fake_send(org, message):
        if sink is not None:
            sink.append((org, message))
        return result
    monkeypatch.setattr(executor.google_client, "send_gmail", fake_send)


# ── auth ──

def test_approve_requires_login(client):
    _seed_action(settings.demo_org_id, "a1", _EMAIL_TYPED)
    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 401  # no cookie → login required


# ── flag OFF, no Cedric: nothing can run, so approve FAILS honestly ──

def test_flag_off_cedricless_org_is_tracked_only(client, monkeypatch):
    """Native off AND the org never linked the Slack agent -> approving RECORDS
    the decision as a track-only card (owner rule 2026-07-22: Cedric lives
    inside Slack — a cedric-less org never sees a doomed dispatch or a
    'couldn't complete')."""
    user = _login(client)
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is True and body["executed"] is False
    assert not calls  # google was never called with the flag off
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st and st["status"] == "approved"
    assert "tracked only" in st["detail"]


def _link_cedric(org: str) -> None:
    """Give the org a connected Slack agent so the dispatch path is exercised."""
    store.set_connection(org, "cedric", "cedric-brain", "connected", {})


def test_flag_off_linked_org_dead_end_marks_failed_with_reason(client, monkeypatch):
    """Org WITH the Slack agent linked but Cedric unreachable/unconfigured ->
    the honest failed receipt with a readable reason (owner ask 2026-07-20)."""
    user = _login(client)
    _link_cedric(user["org_id"])
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is True and body["executed"] is False
    assert not calls
    assert body["dispatch_reason"] == "not_configured"
    assert "Cedric isn't connected" in body["execution_error"]
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st and st["status"] == "failed"
    assert "Cedric isn't connected" in st["detail"]


def test_dead_end_dispatch_endpoint_missing_surfaces_reason(client, monkeypatch):
    """Cedric configured but its action receiver isn't built (404) -> the
    approve reports exactly that instead of a silent success."""
    from app.cedric import callback as cedric_callback

    user = _login(client)
    _link_cedric(user["org_id"])
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    monkeypatch.setattr(
        cedric_callback, "dispatch_action",
        lambda org, action, approved_by="": {"ok": False,
                                              "reason": "dispatch_endpoint_missing"},
    )
    r = client.post("/dashboard/actions/a1/approve")
    body = r.json()
    assert body["executed"] is False and body["dispatched"] is False
    assert body["dispatch_reason"] == "dispatch_endpoint_missing"
    assert "receiver isn't built" in body["execution_error"]
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st["status"] == "failed"


def test_dispatch_ok_marks_approved_routed_to_cedric(client, monkeypatch):
    """Cedric accepts the dispatch -> the action is 'approved · with Cedric',
    NOT failed; the receipt flips to done/failed when Cedric reports back."""
    from app.cedric import callback as cedric_callback

    user = _login(client)
    _link_cedric(user["org_id"])
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    monkeypatch.setattr(
        cedric_callback, "dispatch_action",
        lambda org, action, approved_by="": {"ok": True, "accepted": True},
    )
    r = client.post("/dashboard/actions/a1/approve")
    body = r.json()
    assert body["executed"] is False and body["dispatched"] is True
    assert body["execution_error"] == ""
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st["status"] == "approved"


# ── flag ON + typed: the executor runs and writes the receipt ──

def test_flag_on_executes_typed_email_and_writes_receipt(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m-123"}, calls)

    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is True and body["typed"] is True
    assert body["execution_mode"] == "native"
    assert len(calls) == 1 and calls[0][0] == user["org_id"]
    # Receipt: done + the message id, in the channel the dashboard reads.
    assert body["status"]["status"] == "done"
    assert "m-123" in body["status"]["detail"]
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st["status"] == "done" and "m-123" in st["detail"]


def test_flag_on_untyped_action_gets_retyped_on_approve_and_executes(
    client, monkeypatch
):
    """Approve-time rescue (live 2026-07-21, action e974c47c): an action that
    reached approval untyped — fast click before finalize, or typing raced a
    deploy — is re-typed by the SAME type_actions pass at the approve door.
    'Email the recap to marco@acme.com' types to email.send; the stub-typed
    spec lacks subject/body, so the flow lands on the needs_details edit
    affordance (422 + the exact missing fields) instead of the old doomed
    cedric route ('couldn't complete — nothing ran')."""
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    _seed_action(user["org_id"], "a1", typed=None)  # no typed spec yet
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 422  # rescued into the edit flow, not doomed
    body = r.json()
    assert body["error"] == "needs_details" and body["missing_params"]
    assert not calls  # nothing executed until the human fills the gaps
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st["status"] == "needs_details"  # never 'failed', never cedric


def test_flag_on_untypeable_action_tracked_only_without_cedric(
    client, monkeypatch
):
    """Flag on, the action can't be typed even at the approve door (no mappable
    intent), and the org has no Slack agent -> a track-only approved card
    (owner rule 2026-07-22), never a doomed dispatch, never a google call."""
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    action = {"item": "Sort out the vendor situation", "owner": "Ben",
              "action_id": "a1"}
    store.save_artifact(
        "bot_a1",
        {"summary": "Kickoff.", "actions": [action], "checklist": [action],
         "org_id": user["org_id"], "avatar_id": "laura",
         "meeting_url": "https://meet.google.com/appr-test",
         "transcript": "PII must never leak"},
        org_id=user["org_id"],
    )
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is False and body["typed"] is False
    assert not calls
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st["status"] == "approved"
    assert "tracked only" in st["detail"]


def test_untyped_calendar_ask_goes_to_needs_details_not_tracked(
    client, monkeypatch
):
    """Live 2026-07-23 bug ③: an org WITHOUT Asana connected captured a calendar
    ask. type_actions(allow_asana=False) returns None, so the old flow dropped
    it to 'approved · tracked only — no executor'. Now the approve door
    SYNTHESISES calendar.create_event and lands on needs_details (collect
    time/attendees) — never a tracked-only dead end for a supported action type.
    The synthesised type is PERSISTED so the params form can save against it."""
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    action = {"item": "Schedule a follow-up call with the client next Tuesday",
              "owner": "Ben", "action_id": "a1"}
    store.save_artifact(
        "bot_a1",
        {"summary": "Kickoff.", "actions": [action], "checklist": [action],
         "org_id": user["org_id"], "avatar_id": "laura",
         "meeting_url": "https://meet.google.com/appr-test",
         "transcript": "PII must never leak"},
        org_id=user["org_id"],
    )
    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 422  # needs_details, NOT a tracked-only dead end
    body = r.json()
    assert body["error"] == "needs_details" and body["missing_params"]
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st["status"] == "needs_details"
    # the synthesised type was PERSISTED — the params form saves against it
    typed = ledger.effective_typed("a1", None, org_id=user["org_id"])
    assert typed and typed["type"] == "calendar.create_event"


def test_flag_on_soft_failure_records_failed_receipt(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    _mock_send(monkeypatch, {"ok": False, "error": "gmail send failed (HTTP 500)"})

    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is True  # we attempted it
    assert body["status"]["status"] == "failed" and "500" in body["status"]["detail"]


# ── org scoping ──

def test_cannot_approve_another_orgs_action(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    owner = store.upsert_user("owner@x.com")
    _seed_action(owner["org_id"], "a1", _EMAIL_TYPED)
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    # A DIFFERENT user (different personal org) tries to approve it.
    _login(client, "intruder@y.com")
    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 404
    assert not calls  # never executed for a foreign org


def test_unknown_action_is_404(client):
    _login(client)
    r = client.post("/dashboard/actions/does-not-exist/approve")
    assert r.status_code == 404


# ── retry: a failed receipt is re-approvable and really re-dispatches ──

def test_reapprove_after_failure_retries_dispatch(client, monkeypatch):
    """The failed row's Approve button must RETRY (live repro 2026-07-20: the
    replay branch answered from the first-write-wins decision record and never
    re-called dispatch_action — the action was permanently stuck at 'failed'
    while the UI kept offering an Approve that did nothing)."""
    from app.cedric import callback as cedric_callback

    user = _login(client)
    _link_cedric(user["org_id"])  # dispatch path needs a linked Slack agent
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    calls: list = []

    def flaky_dispatch(org, action, approved_by=""):
        calls.append(org)
        if len(calls) == 1:  # Cedric offline on the first approve…
            return {"ok": False, "reason": "not_configured"}
        return {"ok": True, "accepted": True}  # …reconnected on the retry

    monkeypatch.setattr(cedric_callback, "dispatch_action", flaky_dispatch)

    first = client.post("/dashboard/actions/a1/approve").json()
    assert first["dispatched"] is False
    assert ledger.action_statuses(
        ["a1"], org_id=user["org_id"])["a1"]["status"] == "failed"

    second = client.post("/dashboard/actions/a1/approve").json()
    assert len(calls) == 2, "re-approve must re-dispatch, not replay"
    assert not second.get("idempotent_replay")
    assert second["dispatched"] is True and second["execution_error"] == ""
    st = ledger.action_statuses(["a1"], org_id=user["org_id"])["a1"]
    assert st["status"] == "approved"


def test_reapprove_after_done_stays_an_idempotent_replay(client, monkeypatch):
    """Only 'failed' is re-approvable; a done receipt must never re-execute."""
    user = _login(client)
    monkeypatch.setattr(settings, "native_executor", True)
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    first = client.post("/dashboard/actions/a1/approve").json()
    assert first["executed"] is True and len(calls) == 1
    assert ledger.action_statuses(
        ["a1"], org_id=user["org_id"])["a1"]["status"] == "done"

    second = client.post("/dashboard/actions/a1/approve").json()
    assert second["idempotent_replay"] is True
    assert len(calls) == 1, "a done action must never execute twice"


def test_reopen_failed_action_is_a_narrow_cas(client):
    """failed→approved through the explicit door only; done stays immutable,
    and the general monotonic guard still refuses to un-fail on its own."""
    user = _login(client)
    org = user["org_id"]
    ledger.set_action_status("x1", "failed", "boom", org_id=org)
    # The general status channel cannot repaint a terminal receipt…
    ledger.set_action_status("x1", "approved", "late replay", org_id=org)
    assert ledger.action_statuses(["x1"], org_id=org)["x1"]["status"] == "failed"
    # …but the explicit reopen CAS can, exactly once per failure.
    assert ledger.reopen_failed_action("x1", org_id=org) is True
    assert ledger.action_statuses(["x1"], org_id=org)["x1"]["status"] == "approved"
    assert ledger.reopen_failed_action("x1", org_id=org) is False
    ledger.set_action_status("x1", "done", "", org_id=org)
    assert ledger.reopen_failed_action("x1", org_id=org) is False


def test_browser_route_with_operator_off_fails_with_reason(client):
    """route='browser' + operator disabled: the approve must say WHY as a
    failed receipt — the same honesty 8e6e15a gave the Cedric route — instead
    of returning a silent 'approved' nothing will ever run."""
    user = _login(client)
    action = {"item": "Submit the portal form", "owner": "Ben",
              "action_id": "b1", "execution_route": "browser"}
    store.save_artifact(
        "bot_b1",
        {"summary": "Kickoff.", "actions": [action], "checklist": [action],
         "org_id": user["org_id"], "avatar_id": "laura",
         "meeting_url": "https://meet.google.com/appr-test",
         "transcript": "PII must never leak"},
        org_id=user["org_id"],
    )
    body = client.post("/dashboard/actions/b1/approve").json()
    assert body["executed"] is False
    assert "browser operator is switched off" in body["execution_error"]
    st = ledger.action_statuses(["b1"], org_id=user["org_id"])["b1"]
    assert st["status"] == "failed"
    assert "browser operator" in st["detail"]


def test_rescue_typing_receives_the_meeting_brief(client, monkeypatch):
    """The approve-door retype must see the artifact SUMMARY: clarify-given
    details (times, emails) live there, not in the card text (live
    2026-07-22 — 'schedule a meeting for tomorrow with Anant' typed to
    nothing without them)."""
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    action = {"item": "Can you schedule a meeting for tomorrow with Anant?",
              "owner": "", "action_id": "a1"}
    store.save_artifact(
        "bot_a1",
        {"summary": "Agreed: meeting tomorrow at 3 PM with anant@sffstudio.com.",
         "actions": [action], "checklist": [action],
         "org_id": user["org_id"], "avatar_id": "petra",
         "meeting_url": "https://meet.google.com/appr-test",
         "transcript": "PII must never leak"},
        org_id=user["org_id"],
    )
    from app.brain import engine as brain_engine

    seen = {}

    def fake_type_actions(actions, brief="", **kw):
        seen["brief"] = brief
        seen["item"] = actions[0].get("item") if actions else ""
        return list(actions)  # untyped: flow proceeds to tracked-only

    monkeypatch.setattr(brain_engine, "type_actions", fake_type_actions)
    r = client.post("/dashboard/actions/a1/approve")
    # The retype still saw the brief; and now that the retype declined, the
    # synthesised calendar spec routes this to needs_details (bug ③ fix) instead
    # of the old tracked-only dead end this test used to assert.
    assert r.status_code == 422 and r.json()["error"] == "needs_details"
    assert "3 PM with anant@sffstudio.com" in seen["brief"]
    assert "schedule a meeting" in seen["item"].lower()


def test_native_receipt_carries_verified_on_readback(client, monkeypatch):
    """Native-plane read-back (Phase-1 parity with Pipedream): when the
    just-sent message re-reads OK, the receipt says '· verified'."""
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    _mock_send(monkeypatch, {"ok": True, "message_id": "m-777"})
    monkeypatch.setattr(executor.google_client, "verify_gmail_message",
                        lambda org, mid: mid == "m-777")

    r = client.post("/dashboard/actions/a1/approve")
    body = r.json()
    assert body["executed"] is True
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st["status"] == "done"
    assert "· verified" in st["detail"] and "m-777" in st["detail"]


def test_untyped_but_already_done_action_replays_idempotently(
    client, monkeypatch
):
    """Adversarial review 2026-07-23: an action decided/executed through ANOTHER
    door (Cedric webhook) can be untyped here yet already 'done'. Re-approve
    (double-click, stale tab) must replay idempotently — NOT trip the untyped
    synthesis into a bogus 422 'add details' form for work that already ran."""
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    action = {"item": "Schedule a follow-up call with the client next Tuesday",
              "owner": "Ben", "action_id": "a1"}
    store.save_artifact(
        "bot_a1",
        {"summary": "Kickoff.", "actions": [action], "checklist": [action],
         "org_id": user["org_id"], "avatar_id": "laura",
         "meeting_url": "https://meet.google.com/appr-test",
         "transcript": "PII must never leak"},
        org_id=user["org_id"],
    )
    ledger.set_action_status("a1", "done", "done via cedric",
                             org_id=user["org_id"])
    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code != 422, r.json()  # never a needs_details form on done
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st["status"] == "done"  # monotonic status untouched


def test_untyped_failed_action_keeps_its_retry_path(client, monkeypatch):
    """Same guard for 'failed': the documented failed→reopen-and-retry flow
    (2026-07-20) must not be pre-empted by the untyped synthesis."""
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    action = {"item": "Schedule a follow-up call with the client next Tuesday",
              "owner": "Ben", "action_id": "a1"}
    store.save_artifact(
        "bot_a1",
        {"summary": "Kickoff.", "actions": [action], "checklist": [action],
         "org_id": user["org_id"], "avatar_id": "laura",
         "meeting_url": "https://meet.google.com/appr-test",
         "transcript": "PII must never leak"},
        org_id=user["org_id"],
    )
    ledger.set_action_status("a1", "failed", "vendor 500",
                             org_id=user["org_id"])
    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code != 422, r.json()  # retry path, not an edit form
