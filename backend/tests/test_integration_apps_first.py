"""The seam between the connected-app plane and the meeting surface.

Both halves are covered on their own: ``test_openclaw_connected_apps`` proves
the generic plane (registry, policy funnel, approval gate, tenancy) and
``test_openclaw_meeting_routing`` proves the meeting routing (page intent,
typed action, approval form, agent copy). What neither can prove alone is that
they COMPOSE — that an app the org connected through app_policy actually
reaches the avatar's mouth, and that a page write born in a meeting is still
stopped by the plane's approval gate and its tenancy check.

The harness is the connected-app suite's own FakePipedream, deliberately
reused rather than re-invented: it is strict about tenancy (it raises when an
account id is used by an org it was not minted for), and the plan-gated
pre-built catalog is wired to fail, so nothing here can quietly depend on a
Pipedream plan tier. Key-free, network-free, synthetic.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipedream_executor, store  # noqa: E402
from app.actions import action_plane, app_policy, ledger  # noqa: E402
from app.api import dashboard, voice_agent  # noqa: E402
from app.brain import capabilities, engine, tool_registry, tools  # noqa: E402
from app.openclaw import runtime  # noqa: E402

# The shared harness. The fixtures are imported by NAME so pytest resolves them
# in this module exactly as it does in the suite they come from.
from test_openclaw_connected_apps import (  # noqa: E402,F401
    ORG_A,
    ORG_B,
    FakePipedream,
    _Avatar,
    _install,
    fresh_store,
    openclaw,
)

BRIEF = "The team agreed the OpenClaw rollout steps and who owns each one."

# The ask as the room says it. No title is stated, which is the common shape:
# it must still be a Notion page write, and the approval card must be the one
# that asks for a page title.
ASK_UNTITLED = (
    "Create a Notion page with this meeting summary and three checklist items"
)
# The same ask with the page named.
ASK_TITLED = (
    "Create a Notion page called OpenClaw rollout with this meeting summary "
    "and three checklist items"
)


def _session(reg: dict, **kw) -> SimpleNamespace:
    return SimpleNamespace(
        bot_id="bot-integration",
        org_id=ORG_A,
        queued_actions=[],
        asana_live=False,
        asana_snapshot="",
        meeting_url="https://meet.example/integration",
        integration={},
        roster=lambda: [],
        tool_registry=reg,
        **kw,
    )


# ── 1. a newly connected Notion account reaches the meeting agent ────────
def test_newly_connected_notion_is_visible_to_the_meeting_agent(
    openclaw, monkeypatch
):
    """Connect Notion, touch nothing else, and the avatar can speak about it.

    This is the whole point of the integration: the org's catalog
    (app_policy) is what the meeting surface reads, so there is no second
    place to update and no toggle to remember.
    """
    _install(monkeypatch, FakePipedream({ORG_A: ["notion"]}))

    # Nobody has ever set a capability row for this avatar.
    assert store.get_avatar_capabilities("laura", ORG_A) == {}

    entry = app_policy.entry_for(ORG_A, "notion")
    assert entry is not None
    assert entry["can_write"] is True
    assert entry["requires_approval"] is True
    assert "notion.create_page" in entry["deterministic_types"]

    # …the tool registry offers it (verbs from the executor's own mapper, so
    # the plan-gated pre-built catalog — wired to raise — is never needed)…
    reg = tool_registry.assemble(ORG_A, _Avatar())
    assert [a["slug"] for a in reg["pd_apps"]] == ["notion"]
    assert "create page" in reg["pd_apps"][0]["actions"]
    assert reg["pd_org_available"] == []
    assert "Notion" in tool_registry.brief(reg)

    # …the meeting prompt carries it…
    prompt = voice_agent.build_init_payload(
        _session(reg),
        SimpleNamespace(
            name="Cedric", persona_prompt="You are Cedric.",
            voice_agent_language="en",
        ),
    )["conversation_config_override"]["agent"]["prompt"]["prompt"]
    assert "connected_apps" in prompt
    assert "notion" in prompt

    # …and the deterministic spoken answer never denies it.
    snap = capabilities.snapshot("cedric", ORG_A, _session(reg))
    answer = capabilities.answer("Is Notion connected?", snap)
    assert "Notion is connected" in answer
    assert "isn't connected" not in answer


def test_an_app_the_org_has_not_connected_is_not_promised(openclaw, monkeypatch):
    """The mirror image: awareness follows the catalog in BOTH directions."""
    _install(monkeypatch, FakePipedream({ORG_A: ["github"]}))
    reg = tool_registry.assemble(ORG_A, _Avatar())
    assert "notion" not in [a["slug"] for a in reg["pd_apps"]]
    snap = capabilities.snapshot("cedric", ORG_A, _session(reg))
    answer = capabilities.answer("Can you create a page in Notion?", snap)
    assert "Notion isn't connected for this workspace" in answer
    # …but it still offers the honest alternative rather than a flat refusal.
    assert "capture the action" in answer


# ── 2. the ask becomes notion.create_page, never Calendar ────────────────
def test_the_meeting_ask_becomes_a_notion_page_never_a_calendar_event(
    openclaw, monkeypatch
):
    _install(monkeypatch, FakePipedream({ORG_A: ["notion"]}))
    connected = app_policy.connected_slugs(ORG_A)
    assert connected == ["notion"]

    for ask in (ASK_UNTITLED, ASK_TITLED):
        kind = tools.ask_kind(ask, connected_apps=connected)
        assert kind == "notion", ask
        # The live clarify loop asks the room for nothing — above all not a time.
        assert tools.missing_action_details(ask, kind) == [], ask
        # …and whatever the approve door has to synthesise, it is never Calendar.
        assert dashboard._SYNTH_TYPE_BY_KIND[kind] == "notion.create_page"

    # Named page → fully typed, with the summary and the three checklist items.
    typed = engine.type_actions(
        [{"item": ASK_TITLED}], BRIEF, provider="stub"
    )[0]["typed"]
    assert typed["type"] == "notion.create_page"
    assert typed["args"]["title"] == "OpenClaw rollout"
    assert BRIEF in typed["args"]["content"]
    assert typed["args"]["content"].count("- [ ]") == 3
    assert not {"start", "end", "attendees"} & set(typed["args"])
    assert action_plane.missing_params(typed) == []

    # Unnamed page → deliberately untyped, so the approve door opens a form
    # asking for the PAGE TITLE. Never a start and end time.
    assert engine.type_actions(
        [{"item": ASK_UNTITLED}], BRIEF, provider="stub"
    )[0].get("typed") is None
    synth = {"type": dashboard._SYNTH_TYPE_BY_KIND["notion"], "args": {}}
    assert action_plane.missing_params(synth) == ["title"]


def test_live_capture_routes_the_ask_through_the_org_catalog(
    openclaw, monkeypatch
):
    """The live tool call, end to end: queued, approval-gated, no time asked."""
    captured: dict = {}

    def _capture_once(_session, text, *_a, **_k):
        captured["text"] = text
        return {"action_id": "act-int", "action": text}, True

    _install(monkeypatch, FakePipedream({ORG_A: ["notion"]}))
    monkeypatch.setattr(tools, "capture_action_once", _capture_once)
    reg = tool_registry.assemble(ORG_A, _Avatar())

    result = voice_agent._tool_queue_action(
        _session(reg), {"summary": ASK_TITLED, "request_id": "req-int"}, "call-int"
    )

    assert result["status"] == "queued"
    assert result["approval_required"] is True
    assert "missing" not in result
    assert "OpenClaw rollout" in captured["text"]


# ── 3. a write cannot execute before approval ────────────────────────────
def _notion_run(action_id: str, org: str) -> dict:
    return {
        "summary": BRIEF,
        "decisions": [],
        "actions": [
            {
                "action_id": action_id,
                "item": ASK_TITLED,
                "owner": "Cedric",
                "typed": {
                    "type": "notion.create_page",
                    "args": {"title": "OpenClaw rollout", "content": "Synthetic"},
                },
            }
        ],
        "avatar_id": "cedric",
        "org_id": org,
        "meeting_url": "https://meet.example/integration",
    }


def test_a_notion_write_cannot_execute_before_approval(openclaw, monkeypatch):
    """A valid run capability is not approval — for the meeting's own action."""
    fake = _install(monkeypatch, FakePipedream({ORG_A: ["notion"]}))
    created = runtime.create_meeting_run(
        "bot_int_unapproved", _notion_run("oc_int_unapproved", ORG_A), ORG_A
    )
    run_id = created["run"]["run_id"]
    token = runtime.mint_capability(ORG_A, run_id)

    blocked = runtime.run_tool(
        token,
        "pipedream_run_app_action",
        {"action_id": "oc_int_unapproved", "step_id": "step-1"},
    )

    assert blocked["ok"] is False
    assert blocked["status"] == 403
    assert "has not been approved" in blocked["error"]
    assert fake.proxy_calls == []          # nothing reached the vendor
    assert runtime.run_detail(ORG_A, run_id)["actions"][0]["status"] == "queued"

    # After the explicit decision it runs — exactly once, with a receipt.
    assert runtime.approve_action(ORG_A, "oc_int_unapproved", start=False)["ok"]
    first = runtime.run_tool(
        token, "pipedream_run_app_action",
        {"action_id": "oc_int_unapproved", "step_id": "step-1"},
    )
    second = runtime.run_tool(
        token, "pipedream_run_app_action",
        {"action_id": "oc_int_unapproved", "step_id": "step-2"},
    )
    assert first["ok"] is True and first["replay"] is False
    assert second["replay"] is True
    # Exactly one SIDE EFFECT. The executor also issues a read-back GET to
    # verify what it wrote ("provider readback" on the receipt), so counting
    # every proxy call would count that too — the invariant is one write.
    writes = [c for c in fake.proxy_calls if c["method"] == "POST"]
    reads = [c for c in fake.proxy_calls if c["method"] == "GET"]
    assert len(writes) == 1
    assert writes[0]["url"] == "https://api.notion.com/v1/pages"
    assert [c["url"] for c in reads] == ["https://api.notion.com/v1/pages/obj_1"]
    assert ledger.action_statuses(
        ["oc_int_unapproved"], org_id=ORG_A
    )["oc_int_unapproved"]["status"] == "done"


# ── 4. cross-organization account ids are rejected ───────────────────────
def test_cross_organization_account_ids_are_rejected(openclaw, monkeypatch):
    """Both orgs connect the SAME app, so only the account id distinguishes
    them — the sharp version of the tenancy question."""
    fake = _install(
        monkeypatch, FakePipedream({ORG_A: ["notion"], ORG_B: ["notion"]})
    )

    # Each org's own read goes through its OWN account. FakePipedream raises on
    # any mismatch, so a leak fails the test rather than passing quietly.
    for org in (ORG_A, ORG_B):
        assert pipedream_executor.read_proxy_for_planner(
            org,
            {"app": "notion", "method": "POST",
             "url": "https://api.notion.com/v1/search"},
        )["ok"] is True
    assert {c["account_id"] for c in fake.proxy_calls} == {
        f"acct_{ORG_A}_notion", f"acct_{ORG_B}_notion"
    }

    # An org-B capability cannot drive org A's approved Notion write: org A's
    # run does not exist in org B's namespace, so the token never resolves.
    created = runtime.create_meeting_run(
        "bot_int_org_a", _notion_run("oc_int_org_a", ORG_A), ORG_A
    )
    runtime.approve_action(ORG_A, "oc_int_org_a", start=False)
    before = len(fake.proxy_calls)
    foreign = runtime.mint_capability(ORG_B, created["run"]["run_id"])

    result = runtime.run_tool(
        foreign,
        "pipedream_run_app_action",
        {"action_id": "oc_int_org_a", "step_id": "step-1"},
    )

    assert result["ok"] is False
    assert (result["status"], result["error"]) == (401, "invalid capability")
    assert runtime.get_run(ORG_B, created["run"]["run_id"]) is None
    assert len(fake.proxy_calls) == before      # the approved write never ran

    # And the meeting surface is isolated the same way: org B connecting Notion
    # tells org A's avatar nothing.
    fake.accounts_by_org = {ORG_A: [], ORG_B: ["notion"]}
    pipedream_executor._reset_conn_cache()
    reg_a = tool_registry.assemble(ORG_A, _Avatar())
    assert reg_a["pd_apps"] == [] and reg_a["pd_org_available"] == []
