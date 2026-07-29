"""The demo, end to end, exactly as it is performed.

One meeting ask — "Create a Notion page called OpenClaw Demo, add a short
meeting summary, and add a checklist with the next three steps." — walked from
capture to a verified receipt, plus the chat that reads that same meeting.

The seven steps this file pins, in order:

  1. the meeting asks for the page;
  2. the captured type is ``notion.create_page`` with ONLY title + content;
  3. it lands under the right meeting in the Action Center;
  4. there are ZERO vendor writes before approval;
  5. approval performs exactly ONE Notion write;
  6. the read-back produces a VERIFIED receipt;
  7. a chat bound to that meeting answers about it and PROPOSES a follow-up
     without executing anything.

Steps 1-6 and step 7 run in two different configurations on purpose, because
that is how the product really behaves: the Action Center demo needs no
OpenClaw gateway, while Chat is gated on the experiment being enabled for the
org. ``docs/OPENCLAW-DEMO-RUNBOOK.md`` documents both.

Key-free throughout: the stub brain types the action, a fake Pipedream is the
whole Connect surface, and a fake gateway answers the chat.
"""
from __future__ import annotations

import importlib
import json
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
from app.openclaw import runtime  # noqa: E402

DEMO_ASK = (
    "Create a Notion page called OpenClaw Demo, add a short meeting summary, "
    "and add a checklist with the next three steps."
)
DEMO_TITLE = "OpenClaw Demo"
BRIEF = (
    "The team walked through the OpenClaw connected-app plane and agreed the "
    "next three steps before the pilot."
)
MEETING_ID = "bot_openclaw_demo"
ACTION_ID = "act_openclaw_demo"
OTHER_MEETING_ID = "bot_unrelated_demo"
OTHER_ACTION_ID = "act_unrelated_demo"

PAGE_ID = "22222222-3333-4444-5555-666666666666"


class FakeNotion:
    """The org's Connect account plane + proxy, recording every request."""

    def __init__(self, org: str):
        self.org = org
        self.calls: list[dict] = []

    @property
    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] != "GET"]

    @property
    def reads(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "GET"]

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
        if method == "GET":
            # The read-back: the page really exists after the write.
            return {"status": 200, "ok": True, "json": {"id": PAGE_ID}}
        return {"status": 200, "ok": True,
                "json": {"id": PAGE_ID,
                         "url": f"https://www.notion.so/{PAGE_ID}"}}


def _connect_notion(monkeypatch, org: str) -> FakeNotion:
    fake = FakeNotion(org)
    monkeypatch.setattr(settings, "pipedream_project_id", "proj_demo")
    monkeypatch.setattr(settings, "pipedream_client_id", "cid")
    monkeypatch.setattr(settings, "pipedream_client_secret", "sec")
    monkeypatch.setattr(settings, "pipedream_executor", True)
    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(pipedream_client, "list_accounts", fake.list_accounts)
    monkeypatch.setattr(pipedream_client, "proxy_request", fake.proxy_request)
    # The plan-gated pre-built catalog must never be required by the demo.
    monkeypatch.setattr(
        pipedream_client, "list_actions",
        lambda *_a, **_k: (_ for _ in ()).throw(
            pipedream_client.PipedreamError("not available on your current plan")
        ),
    )
    monkeypatch.setattr(pipedream_client, "plan_gated", lambda: True)
    pipedream_executor._reset_conn_cache()
    return fake


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    importlib.reload(outbox)
    runtime._SCHEMA_READY = False

    client = TestClient(main_module.app)
    user = store.upsert_user("demo-owner@example.com")
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    org = str(user["org_id"])
    return {"client": client, "org": org, "user": user,
            "fake": _connect_notion(monkeypatch, org)}


def _save_meeting(org: str, bot_id: str, ask: str, action_id: str) -> dict:
    """Capture → finalize typing pass → artifact, the production path."""
    typed_actions = engine.type_actions(
        [{"action_id": action_id, "item": ask, "owner": "Cedric"}],
        BRIEF,
        provider="stub",
    )
    artifact = {
        "summary": BRIEF,
        "decisions": ["Ship the connected-app plane behind approval."],
        "actions": typed_actions,
        "checklist": typed_actions,
        "avatar_id": "cedric",
        "org_id": org,
        "meeting_url": f"https://meet.google.com/{bot_id}",
        "meeting_type": "working_session",
        "readiness_score": 82,
    }
    store.save_artifact(bot_id, artifact, org_id=org)
    return artifact


def test_demo_steps_1_to_6_capture_to_verified_receipt(demo):
    org, client, fake = demo["org"], demo["client"], demo["fake"]

    # ── 1 + 2. the ask is captured as a Notion page, title + content only ──
    artifact = _save_meeting(org, MEETING_ID, DEMO_ASK, ACTION_ID)
    # A second, unrelated meeting exists so step 3 proves GROUPING, not luck.
    _save_meeting(
        org, OTHER_MEETING_ID,
        "Create a Notion page called Vendor Review with the meeting summary",
        OTHER_ACTION_ID,
    )
    typed = artifact["actions"][0]["typed"]

    assert typed["type"] == "notion.create_page"
    assert set(typed["args"]) == {"title", "content"}
    assert typed["args"]["title"] == DEMO_TITLE
    assert action_plane.missing_params(typed) == []
    content = typed["args"]["content"]
    assert "Meeting summary" in content and BRIEF in content
    assert content.count("- [ ] ") == 3

    # ── 3. it appears under the RIGHT meeting in the Action Center ─────────
    summary = client.get("/dashboard/summary")
    assert summary.status_code == 200
    meetings = {m["bot_id"]: m for m in summary.json()["meetings"]}
    assert MEETING_ID in meetings and OTHER_MEETING_ID in meetings
    assert [a["action_id"] for a in meetings[MEETING_ID]["actions"]] == [ACTION_ID]
    assert [a["action_id"] for a in meetings[OTHER_MEETING_ID]["actions"]] == [
        OTHER_ACTION_ID
    ]
    entry = meetings[MEETING_ID]["actions"][0]
    assert entry["typed"] is True and entry["done"] is False

    # ── 4. ZERO vendor writes before approval ─────────────────────────────
    assert fake.calls == []
    assert ledger.get_action_decision(ACTION_ID, org_id=org) is None

    # ── 5. approval performs exactly ONE Notion write ─────────────────────
    approved = client.post(f"/dashboard/actions/{ACTION_ID}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved"] is True
    assert approved.json()["executed"] is True

    assert len(fake.writes) == 1
    write = fake.writes[0]
    assert write["method"] == "POST"
    assert write["url"] == "https://api.notion.com/v1/pages"
    assert DEMO_TITLE in json.dumps(write["body"])
    # The sibling meeting's action was never touched.
    assert ledger.get_action_decision(OTHER_ACTION_ID, org_id=org) is None

    # ── 6. the read-back produces a VERIFIED receipt ──────────────────────
    # The executor re-reads the page it just wrote, so the receipt is evidence
    # rather than presumption.
    assert [r["url"] for r in fake.reads] == [
        f"https://api.notion.com/v1/pages/{PAGE_ID}"
    ]
    status = ledger.action_statuses([ACTION_ID], org_id=org)[ACTION_ID]
    assert status["status"] == "done"
    # The STRUCTURED receipt (verified/verification/route as fields) is
    # persisted on the Postgres control plane only. In the key-free SQLite mode
    # the demo runs in, the same evidence lands in the human receipt line —
    # which is also exactly what the Action Center renders.
    detail = status["detail"]
    assert "verified" in detail
    assert PAGE_ID in detail
    assert detail.startswith("Pipedream · notion page · verified")


def test_demo_step_7_meeting_chat_answers_and_proposes_without_executing(
    demo, monkeypatch
):
    org, client, fake = demo["org"], demo["client"], demo["fake"]
    _save_meeting(org, MEETING_ID, DEMO_ASK, ACTION_ID)

    # Chat is gated on the experiment being enabled for this workspace.
    monkeypatch.setattr(settings, "openclaw_experiment_enabled", True)
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", org)
    monkeypatch.setattr(settings, "openclaw_gateway_url", "http://openclaw.test")

    sent: list[dict] = []

    class GatewayResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "id": "resp_demo",
                "output": [{
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": json.dumps({
                            "reply": (
                                "The meeting agreed the next three steps before "
                                "the pilot. I can file the recap page too."
                            ),
                            "workflow": {
                                "title": "File the recap",
                                "summary": "One follow-up page.",
                                "steps": [{
                                    "description": "Create the recap page",
                                    "action_type": "notion.create_page",
                                    "args": {"title": "OpenClaw Demo recap",
                                             "content": "Recap of the demo."},
                                }],
                            },
                        }),
                    }],
                }],
            }

    def _post(url, *, json, headers, timeout):
        sent.append({"url": url, "json": json})
        return GatewayResponse()

    monkeypatch.setattr(main_module.httpx, "post", _post)

    # A chat BOUND to the meeting.
    created = client.post(
        "/dashboard/openclaw/chats", json={"meeting_id": MEETING_ID}
    )
    assert created.status_code == 200
    thread = created.json()["thread"]
    assert thread["meeting_id"] == MEETING_ID

    answered = client.post(
        "/dashboard/openclaw/chat",
        json={"thread_id": thread["thread_id"],
              "message": "What did we agree, and can you file the recap?"},
    )
    assert answered.status_code == 200
    body = answered.json()

    # It answered ABOUT that meeting: the meeting's distilled context was sent,
    # and the raw transcript never was. The gateway call is selected by URL —
    # Pipedream's own OAuth token fetches share this same httpx.post seam.
    gateway_calls = [s for s in sent if "openclaw.test" in s["url"]]
    assert len(gateway_calls) == 1
    payload = json.loads(gateway_calls[0]["json"]["input"])
    assert payload["selected_meeting_id"] == MEETING_ID
    assert payload["meetings"][0]["meeting_id"] == MEETING_ID
    assert payload["raw_transcript_included"] is False
    assert "next three steps" in body["reply"]

    # It PROPOSED a follow-up — reviewable, ready, and NOT executed.
    workflow = body["workflow"]
    assert workflow is not None
    assert [s["action_type"] for s in workflow["steps"]] == ["notion.create_page"]
    assert workflow["ready"] is True
    assert all(step["requires_approval"] for step in workflow["steps"])

    # Nothing ran: no vendor call, no run, no decision.
    assert fake.calls == []
    assert runtime.list_runs(org) == []
    assert ledger.get_action_decision(ACTION_ID, org_id=org) is None

    # The opening message is the simple one the demo shows.
    detail = client.get(
        f"/dashboard/openclaw/chats/{thread['thread_id']}"
    ).json()["thread"]
    assert detail["messages"][0]["text"] == runtime.CHAT_GREETING
    assert runtime.CHAT_GREETING == (
        "If you want to know what I can do, just ask me."
    )


def test_the_two_demo_configurations_are_mutually_exclusive_without_a_gateway(
    demo, monkeypatch
):
    """Pin the trap that would otherwise be found live, mid-demo.

    Enabling the OpenClaw experiment for an org — which Chat REQUIRES — also
    re-routes that org's meeting actions away from the direct connected-app
    executor and onto the OpenClaw runner. The runner only owns actions that
    belong to a run created at meeting-finalize time, so approving an action
    from a seeded artifact answers 409 and settles ``failed`` instead of
    writing anything.

    Nothing here is wrong: refusing to execute an action it cannot account for
    is the correct answer, and no vendor call escapes. But it means the Action
    Center demo and the Chat demo need different flags unless a gateway is
    configured, which is why the runbook splits them.
    """
    org, client, fake = demo["org"], demo["client"], demo["fake"]
    _save_meeting(org, MEETING_ID, DEMO_ASK, ACTION_ID)
    typed = {"type": "notion.create_page", "args": {"title": "x", "content": "y"}}

    from app.actions import executor as action_executor

    assert action_executor.route_for_typed(typed, org) == "pipedream"

    monkeypatch.setattr(settings, "openclaw_experiment_enabled", True)
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", org)
    monkeypatch.setattr(settings, "openclaw_gateway_url", "")
    assert action_executor.route_for_typed(typed, org) == "openclaw"

    refused = client.post(f"/dashboard/actions/{ACTION_ID}/approve")

    assert refused.status_code == 409
    assert ledger.action_statuses([ACTION_ID], org_id=org)[ACTION_ID][
        "status"
    ] == "failed"
    # The safety property that matters: nothing reached the vendor.
    assert fake.calls == []


def test_demo_chats_are_independent_and_deletion_is_permanent(demo, monkeypatch):
    """New chat / multiple chats / permanent Delete chat — the UI's contract."""
    org, client = demo["org"], demo["client"]
    monkeypatch.setattr(settings, "openclaw_experiment_enabled", True)
    monkeypatch.setattr(settings, "openclaw_experiment_orgs", org)
    _save_meeting(org, MEETING_ID, DEMO_ASK, ACTION_ID)

    bound = client.post(
        "/dashboard/openclaw/chats", json={"meeting_id": MEETING_ID}
    ).json()["thread"]
    general = client.post("/dashboard/openclaw/chats", json={}).json()["thread"]
    assert bound["thread_id"] != general["thread_id"]
    assert bound["meeting_id"] == MEETING_ID
    assert general.get("meeting_id") in ("", None)
    assert len(client.get("/dashboard/openclaw/chats").json()["threads"]) == 2

    removed = client.delete(f"/dashboard/openclaw/chats/{bound['thread_id']}")
    assert removed.status_code == 200

    remaining = client.get("/dashboard/openclaw/chats").json()["threads"]
    assert [t["thread_id"] for t in remaining] == [general["thread_id"]]
    # Permanent: the thread and its messages are gone, not archived.
    assert client.get(
        f"/dashboard/openclaw/chats/{bound['thread_id']}"
    ).status_code == 404
    assert runtime.get_chat_thread(org, bound["thread_id"]) is None
