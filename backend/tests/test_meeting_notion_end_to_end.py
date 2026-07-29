"""Deterministic end-to-end for ONE meeting ask, through the real surfaces.

    "Create a Notion page called OpenClaw meeting work, add a short meeting
     summary, and add a checklist with the next three steps."

The unit behaviour of each hop is covered in test_openclaw_meeting_routing.py.
This file is the JOIN: it drives the ask from the finalize typing pass through
the artifact, the dashboard Action Center, the approve door and the connected-app
execution plane, asserting the five properties that actually matter to the room:

  1. the final action is ``notion.create_page``;
  2. it carries ``title`` AND ``content``;
  3. it carries NO calendar parameters anywhere;
  4. it reaches the Action Center awaiting approval;
  5. it CANNOT execute before that approval — and executes exactly once after.

Key-free: the stub brain types the action and a fake Pipedream stands in for the
whole Connect surface. No keys, no network, no real workspace names.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main as main_module  # noqa: E402
from app import auth, pipedream_client, pipedream_executor, store  # noqa: E402
from app.actions import action_plane, ledger, outbox  # noqa: E402
from app.brain import engine  # noqa: E402
from app.config import settings  # noqa: E402
from app.openclaw import runtime as openclaw_runtime  # noqa: E402

ASK = (
    "Create a Notion page called OpenClaw meeting work, add a short meeting "
    "summary, and add a checklist with the next three steps."
)
BRIEF = (
    "The team reviewed the OpenClaw rollout, agreed to ship the connected-app "
    "plane behind approval, and named the next three steps."
)
BOT_ID = "bot_notion_e2e"
ACTION_ID = "act_notion_e2e"

def _params(prefix: str) -> frozenset[str]:
    return frozenset(
        str(f.get("name") or "")
        for action_type, fields in action_plane.PARAMS_SCHEMAS.items()
        if action_type.startswith(prefix)
        for f in fields
        if f.get("name")
    )


# Parameters ONLY the calendar family emits. `title` is deliberately excluded:
# a Notion page has one too, so intersecting the raw calendar schema would flag
# a correct page. What must never appear is a scheduling field — start, end,
# attendees — which is exactly what the live failure produced.
CALENDAR_ONLY_PARAMS = _params("calendar.") - _params("notion.")


class FakeNotion:
    """The Connect account plane + proxy for one org with Notion linked."""

    def __init__(self, org: str):
        self.org = org
        self.calls: list[dict] = []

    def list_accounts(self, external_user_id: str, *, app: str = "") -> list[dict]:
        if str(external_user_id) != self.org:
            return []
        rows = [{"id": f"acct_{self.org}_notion", "app": "notion",
                 "name": "Notion", "healthy": True}]
        return [r for r in rows if not app or r["app"] == app]

    def proxy_request(self, external_user_id, account_id, method, url, **kw):
        if str(account_id) != f"acct_{external_user_id}_notion":
            raise AssertionError("cross-tenant proxy call")
        self.calls.append({"method": method, "url": url,
                           "body": kw.get("json_body")})
        return {"status": 200, "ok": True,
                "json": {"id": "page_1",
                         "url": "https://www.notion.so/page_1"}}


@pytest.fixture
def sim(tmp_path, monkeypatch):
    """A logged-in workspace with Notion connected and nothing else."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    importlib.reload(outbox)
    openclaw_runtime._SCHEMA_READY = False

    client = TestClient(main_module.app)
    user = store.upsert_user("owner@example.com")
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    org = str(user["org_id"])

    fake = FakeNotion(org)
    monkeypatch.setattr(settings, "pipedream_project_id", "proj_test")
    monkeypatch.setattr(settings, "pipedream_client_id", "cid")
    monkeypatch.setattr(settings, "pipedream_client_secret", "sec")
    monkeypatch.setattr(settings, "pipedream_executor", True)
    # execution_mode is derived from native_executor, not settable.
    monkeypatch.setattr(settings, "native_executor", True)
    assert settings.execution_mode == "native"
    monkeypatch.setattr(pipedream_client, "list_accounts", fake.list_accounts)
    monkeypatch.setattr(pipedream_client, "proxy_request", fake.proxy_request)
    # The plan-gated pre-built component catalog must never be needed.
    monkeypatch.setattr(
        pipedream_client, "list_actions",
        lambda *_a, **_k: (_ for _ in ()).throw(
            pipedream_client.PipedreamError("not available on your current plan")
        ),
    )
    monkeypatch.setattr(pipedream_client, "plan_gated", lambda: True)
    pipedream_executor._reset_conn_cache()
    return {"client": client, "org": org, "user": user, "fake": fake}


def _finalize(org: str) -> dict:
    """The finalize typing pass on the captured ask — the production path."""
    captured = [{"action_id": ACTION_ID, "item": ASK, "owner": "Cedric"}]
    typed_actions = engine.type_actions(captured, BRIEF, provider="stub")
    artifact = {
        "summary": BRIEF,
        "decisions": ["Ship the connected-app plane behind approval."],
        "actions": typed_actions,
        "checklist": typed_actions,
        "avatar_id": "cedric",
        "org_id": org,
        "meeting_url": "https://meet.google.com/openclaw-e2e",
        "meeting_type": "working_session",
        "readiness_score": 80,
    }
    store.save_artifact(BOT_ID, artifact, org_id=org)
    return artifact


def test_meeting_ask_becomes_an_approvable_notion_page_and_nothing_else(sim):
    org, client, fake = sim["org"], sim["client"], sim["fake"]

    # ── 1. the ask is typed as a Notion page ────────────────────────────
    artifact = _finalize(org)
    action = artifact["actions"][0]
    typed = action["typed"]

    assert typed["type"] == "notion.create_page"

    # ── 2. title AND content ───────────────────────────────────────────
    args = typed["args"]
    assert args["title"] == "OpenClaw meeting work"
    assert "content" in args and args["content"].strip()
    # Both requested pieces landed in the page body, not in a second action.
    assert "Meeting summary" in args["content"]
    assert BRIEF in args["content"]
    assert "Next steps" in args["content"]
    assert args["content"].count("- [ ] ") == 3
    assert len(artifact["actions"]) == 1

    # ── 3. no calendar parameters, anywhere ────────────────────────────
    assert set(args) & CALENDAR_ONLY_PARAMS == set()
    assert set(args) == {"title", "content"}
    assert action_plane.missing_params(typed) == []
    # The page title is not the whole instruction.
    assert "checklist" not in args["title"].lower()
    assert "summary" not in args["title"].lower()

    # ── 4. it reaches the Action Center awaiting approval ──────────────
    summary = client.get("/dashboard/summary")
    assert summary.status_code == 200
    meeting = next(
        m for m in summary.json()["meetings"] if m["bot_id"] == BOT_ID
    )
    entry = next(a for a in meeting["actions"] if a["action_id"] == ACTION_ID)
    assert entry["typed"] is True
    assert entry["done"] is False
    # Nothing has been decided or executed yet.
    assert ledger.get_action_decision(ACTION_ID, org_id=org) is None
    status = (ledger.action_statuses([ACTION_ID], org_id=org)
              .get(ACTION_ID) or {})
    assert str(status.get("status") or "proposed") == "proposed"
    assert fake.calls == []

    # ── 5a. it cannot execute before approval ──────────────────────────
    # The write is a connected-app write, so the ONLY way to the proxy is the
    # approve door. The low-level executor refuses to be used as a shortcut for
    # an action it has no native adapter for, and nothing calls the vendor.
    from app.actions import executor as native_executor

    direct = native_executor.execute_approved(org, ACTION_ID, typed)
    assert direct["ok"] is False
    assert "unhandled action type" in str(direct.get("skipped") or "")
    assert fake.calls == []
    assert ledger.get_action_decision(ACTION_ID, org_id=org) is None

    # ── 5b. approving is what runs it, exactly once ────────────────────
    approved = client.post(f"/dashboard/actions/{ACTION_ID}/approve")
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["approved"] is True
    assert body["executed"] is True

    # Exactly ONE write. The executor also issues a read-back GET so the
    # receipt is evidence rather than presumption, which is why the call count
    # is measured on writes, not on requests.
    writes = [c for c in fake.calls if c["method"] != "GET"]
    assert len(writes) == 1
    call = writes[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/v1/pages")
    # The page carries the title and the body — and no calendar field.
    sent = call["body"]
    assert "OpenClaw meeting work" in str(sent)
    assert set(sent) & CALENDAR_ONLY_PARAMS == set()
    # No parent was invented: an omitted parent is the private workspace root.
    assert sent["parent"] == {"type": "workspace", "workspace": True}

    decision = ledger.get_action_decision(ACTION_ID, org_id=org)
    assert decision["decision"] == "approve"
    assert decision["decided_via"] == "dashboard"
    final = ledger.action_statuses([ACTION_ID], org_id=org)[ACTION_ID]
    assert final["status"] == "done"

    # A second click replays from the recorded decision — no second page.
    again = client.post(f"/dashboard/actions/{ACTION_ID}/approve")
    assert again.status_code == 200
    assert again.json()["idempotent_replay"] is True
    assert [c for c in fake.calls if c["method"] != "GET"] == writes


def test_the_same_ask_is_never_a_calendar_write_on_any_capture_path(sim):
    """The paraphrase and the ASR-mangled form reach the same typed spec.

    These are the two forms production actually sees: the meeting agent writes
    its own paraphrase into queue_action, and Recall's ASR mangles the product
    name. Both used to type as calendar.create_event and ask the room for a
    start time.
    """
    org = sim["org"]
    variants = {
        "as_spoken": ASK,
        "agent_paraphrase": (
            "Create a Notion page titled OpenClaw meeting work with a short "
            "meeting summary and a checklist of the next three steps"
        ),
        "app_name_dropped": (
            "Create a page called OpenClaw meeting work with a short meeting "
            "summary and a checklist of the next three steps."
        ),
        "asr_mangled": (
            "Create a nation page called OpenClaw meeting work and add a "
            "meeting summary and a checklist"
        ),
    }
    for label, text in variants.items():
        typed_actions = engine.type_actions(
            [{"action_id": f"act_{label}", "item": text, "owner": "Cedric"}],
            BRIEF,
            provider="stub",
        )
        typed = typed_actions[0].get("typed") or {}
        assert typed.get("type") == "notion.create_page", label
        assert typed["args"]["title"] == "OpenClaw meeting work", label
        assert set(typed["args"]) & CALENDAR_ONLY_PARAMS == set(), label
        assert action_plane.missing_params(typed) == [], label
