"""Regression coverage for the connected-app routing of a meeting write.

Live repro (Cedric, OpenClaw org with Notion connected):

    "Cedric, create a Notion page called OpenClow meeting work, add a short
     meeting summary and a checklist with the next three steps."

Three things went wrong at once:

1. Cedric answered that Notion was NOT connected. The avatar's capability
   snapshot was assembled from a hardcoded Gmail/Calendar/Drive/Asana list
   plus Pipedream apps the owner had EXPLICITLY toggled on, so a workspace
   whose Notion the execution plane can actually run was invisible — and the
   spoken roster ("I'm connected to Google Calendar, Gmail and Google Drive")
   read as a denial.
2. The captured action was typed ``calendar.create_event`` and the room was
   asked for a start time. "meeting" in "a short MEETING summary" matched the
   broad calendar vocabulary; when the agent's own paraphrase dropped the app
   name (or ASR heard "a nation page"), nothing outranked it.
3. A Notion ask that stayed untyped hit the approve door's synth table, which
   knew task/email/calendar and not Notion, so it became a tracked-only dead
   end instead of an approvable card.

Everything here is synthetic and vendor-free: no keys, no network, no real
workspace names.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipedream_client, pipedream_executor, store  # noqa: E402
from app.actions import action_plane, app_policy, executor  # noqa: E402
from app.api import voice_agent  # noqa: E402
from app.brain import capabilities, engine, tool_registry, tools  # noqa: E402
from app.config import settings  # noqa: E402


# The ask exactly as the room said it, and the paraphrases that reach the
# capture layer in practice: the ElevenLabs agent writes its OWN summary into
# queue_action, and Recall's ASR mangles product names.
ASK_AS_SPOKEN = (
    "Cedric, create a Notion page called OpenClow meeting work, add a short "
    "meeting summary and a checklist with the next three steps."
)
ASK_AGENT_PARAPHRASE = (
    "Create a Notion page titled OpenClow meeting work with a short meeting "
    "summary and a checklist of the next three steps"
)
ASK_APP_NAME_DROPPED = (
    "Create a page called OpenClow meeting work with a short meeting summary "
    "and a checklist of the next three steps."
)
ASK_ASR_MANGLED = (
    "Create a nation page called OpenClow meeting work and add a meeting "
    "summary and a checklist"
)
ASK_ITALIAN = (
    "Crea una pagina Notion chiamata OpenClow meeting work con un riassunto "
    "della riunione e una checklist"
)
ASK_IN_NOTION = (
    "Add a page in Notion called OpenClow meeting work with the meeting "
    "summary and the next three steps"
)

ALL_ASKS = (
    ASK_AS_SPOKEN,
    ASK_AGENT_PARAPHRASE,
    ASK_APP_NAME_DROPPED,
    ASK_ASR_MANGLED,
    ASK_ITALIAN,
    ASK_IN_NOTION,
)

BRIEF = "The team reviewed the OpenClaw rollout and agreed the next steps."
TITLE = "OpenClow meeting work"


# ── 1. the phrase ────────────────────────────────────────────────────────
def test_page_ask_never_classifies_as_a_calendar_write():
    """Every shape of the ask is a page write, not a meeting to schedule."""
    for ask in ALL_ASKS:
        assert tools.ask_kind(ask) == "notion", ask


def test_page_ask_never_asks_the_room_for_a_start_time():
    """The live symptom: 'when should it be?' for a page-creation request."""
    for ask in ALL_ASKS:
        missing = tools.missing_action_details(ask, tools.ask_kind(ask))
        assert missing == [], (ask, missing)


def test_scheduling_asks_still_route_to_calendar():
    """The widened page intent must not swallow real scheduling asks."""
    for ask in (
        "Schedule a follow-up call with the vendor on Friday at 3pm",
        "Set up a meeting with the security team next week",
        "Book a 30 minute call tomorrow at 10am",
    ):
        assert tools.ask_kind(ask) == "calendar", ask
    missing = tools.missing_action_details(
        "Set up a meeting next week", "calendar"
    )
    assert "invite_when" in missing or "invite_clock" in missing


def test_email_task_and_drive_asks_keep_their_families():
    """No regression for the families that already worked."""
    assert tools.ask_kind("Send an email to sam@example.com with the recap") == "email"
    assert tools.ask_kind("Create a task in Asana to renew the certificate") == "task"
    assert tools.ask_kind("Create a Google Doc called Q3 plan in Drive") == "other"
    assert tools.ask_kind("Share the roadmap file in Drive with the team") == "other"


def test_connected_app_catalog_routes_an_ask_that_names_the_app():
    """Routing follows the ORG's connected apps, not a hardcoded list."""
    assert (
        tools.ask_kind(
            "Add a page for the rollout in Notion", connected_apps=("notion",)
        )
        == "notion"
    )
    # The same ask in a workspace with no Notion is still a page write, not a
    # calendar invite — it just has no connected app behind it.
    assert tools.ask_kind("Add a page for the rollout", connected_apps=()) == "notion"


# ── 2. the typed action ──────────────────────────────────────────────────
def test_typed_action_is_notion_with_clean_title_summary_and_checklist():
    for ask in ALL_ASKS:
        typed = engine.type_actions([{"item": ask}], BRIEF, provider="stub")[0].get(
            "typed"
        )
        assert typed, ask
        assert typed["type"] == "notion.create_page", ask
        # The title is the stated name ONLY — not the whole instruction.
        assert typed["args"]["title"] == TITLE, (ask, typed["args"]["title"])
        # Summary and checklist both survive into the page body.
        content = typed["args"].get("content") or ""
        assert BRIEF in content, ask
        assert content.count("- [ ]") == 3, ask
        # No Calendar parameters anywhere near it.
        assert "start" not in typed["args"] and "end" not in typed["args"], ask
        assert "attendees" not in typed["args"], ask


def test_parent_is_optional_and_never_invented():
    typed = engine.type_actions(
        [{"item": ASK_AS_SPOKEN}], BRIEF, provider="stub"
    )[0]["typed"]
    assert "parent" not in typed["args"]
    # A parent the ask actually names is kept.
    kept = engine._sanitize_typed(
        {"type": "notion.create_page", "args": {"parent": "workspace"}},
        {"item": ASK_AS_SPOKEN},
        BRIEF,
    )
    assert kept["args"]["parent"] == "workspace"
    # A parent nobody mentioned is dropped rather than guessed.
    invented = engine._sanitize_typed(
        {"type": "notion.create_page", "args": {"parent": "Q4 Roadmap Database"}},
        {"item": ASK_AS_SPOKEN},
        BRIEF,
    )
    assert "parent" not in invented["args"]


def test_a_page_title_containing_with_is_not_truncated():
    """The title terminator must cut on a CONTENT clause, not on any 'with'."""
    ask = "Create a Notion page called Sync with the vendor"
    typed = engine.notion_create_spec({"item": ask}, BRIEF)
    assert typed["args"]["title"] == "Sync with the vendor"


def test_notion_intent_overrides_a_model_calendar_guess(monkeypatch):
    monkeypatch.setattr(
        engine,
        "_llm_type_actions",
        lambda *_a, **_k: {
            0: {
                "type": "calendar.create_event",
                "args": {
                    "title": "OpenClow meeting work",
                    "start": "2026-08-01T15:00:00",
                    "end": "2026-08-01T15:30:00",
                },
            }
        },
    )
    typed = engine.type_actions(
        [{"item": ASK_APP_NAME_DROPPED}], BRIEF, provider="synthetic-model"
    )[0]["typed"]
    assert typed["type"] == "notion.create_page"
    assert typed["args"]["title"] == TITLE


def test_calendar_and_email_typing_still_work():
    """Guard rail: the existing typed families are untouched."""
    cal = engine.type_actions(
        [{"item": "Schedule the review from 2026-08-01T15:00:00 to "
                  "2026-08-01T16:00:00"}],
        "",
        provider="stub",
    )[0].get("typed")
    assert cal and cal["type"] == "calendar.create_event"
    mail = engine.type_actions(
        [{"item": "Email the recap to sam@example.com"}], "", provider="stub"
    )[0].get("typed")
    assert mail and mail["type"] == "email.send"
    assert mail["args"]["to"] == ["sam@example.com"]


# ── 3. missing params (the approval card) ────────────────────────────────
def test_missing_params_for_notion_are_the_page_title_only():
    """The card must never ask for when/start/end on a page write."""
    complete = {
        "type": "notion.create_page",
        "args": {"title": TITLE, "content": "Meeting summary\n\nSynthetic."},
    }
    assert action_plane.missing_params(complete) == []

    bare = {"type": "notion.create_page", "args": {}}
    assert action_plane.missing_params(bare) == ["title"]

    fields = {f["name"] for f in action_plane.params_schema(bare)}
    assert fields == {"parent", "title", "content"}
    assert not fields & {"start", "end", "when", "attendees"}
    # parent stays optional
    assert [f["name"] for f in action_plane.params_schema(bare) if f["required"]] == [
        "title"
    ]


def test_untyped_notion_ask_synthesises_a_notion_card_not_a_calendar_one():
    """The approve door's synth table must know the Notion family.

    A page ask with no stated title stays untyped through the typing pass; the
    approve door then synthesises a minimal spec so the human gets a
    needs-details form. Before the fix that table held task/email/calendar
    only, so the ask fell through to a tracked-only dead end (or, once
    mis-classified, to a calendar form asking for a start time).
    """
    from app.api import dashboard

    assert dashboard._SYNTH_TYPE_BY_KIND["notion"] == "notion.create_page"
    assert dashboard._SYNTH_TYPE_BY_KIND["calendar"] == "calendar.create_event"
    assert dashboard._SYNTH_TYPE_BY_KIND["email"] == "email.send"
    assert dashboard._SYNTH_TYPE_BY_KIND["task"] == "asana.create_task"

    vague = "Create a Notion page for the rollout"
    synth = {
        "type": dashboard._SYNTH_TYPE_BY_KIND[tools.ask_kind(vague)],
        "args": {},
    }
    assert synth["type"] == "notion.create_page"
    assert action_plane.missing_params(synth) == ["title"]


# ── 3b. the live capture path, end to end ────────────────────────────────
def test_live_queue_action_files_a_page_ask_without_asking_for_a_time(monkeypatch):
    """The reported journey: the agent calls queue_action with its paraphrase.

    It must be captured as a page write with NO clarify slots, and the card
    must reach the approval queue rather than executing.
    """
    captured: dict = {}

    def _capture_once(_session, text, *_a, **_k):
        captured["text"] = text
        return {"action_id": "act-synthetic", "action": text}, True

    # _tool_queue_action resolves brain.tools lazily, so patching the module
    # itself is what the runtime will see.
    monkeypatch.setattr(tools, "capture_action_once", _capture_once)
    session = SimpleNamespace(
        bot_id="bot-synthetic",
        org_id="org-synthetic",
        queued_actions=[],
        tool_registry={
            "native": [],
            "pd_apps": [{"slug": "notion", "actions": ["create page"]}],
            "pd_org_available": [],
        },
    )
    result = voice_agent._tool_queue_action(
        session,
        {"summary": ASK_AGENT_PARAPHRASE, "request_id": "req-1"},
        "call-1",
    )
    assert result["status"] == "queued"
    assert result["approval_required"] is True
    assert "missing" not in result          # never asks the room for a time
    assert "approved" in result["note"]     # runs only after approval
    assert "OpenClow meeting work" in captured["text"]


# ── 4. capability truth from the org's connected-app catalog ─────────────
def _registry(pd_apps, pd_org_available=()):
    return {
        "native": [
            {"name": "google_calendar", "connected": True, "write": True,
             "verbs": "create event"},
            {"name": "gmail_send", "connected": True, "write": True,
             "verbs": "send"},
        ],
        "pd_apps": list(pd_apps),
        "pd_org_available": list(pd_org_available),
    }


def test_connected_notion_is_never_denied(monkeypatch):
    monkeypatch.setattr(executor, "route_for_typed", lambda *_a, **_k: "openclaw")
    session = SimpleNamespace(
        asana_live=False,
        tool_registry=_registry([{"slug": "notion", "actions": ["create page"]}]),
    )
    snap = capabilities.snapshot("cedric", "org-synthetic", session)
    answer = capabilities.answer("Is Notion connected?", snap)
    assert "Notion is connected" in answer
    assert "isn't connected" not in answer
    # And she says the honest thing about HOW it happens.
    assert "approval" in answer.lower()


def test_an_app_missing_from_the_snapshot_is_not_implicitly_denied():
    """The roster answer used to stand in for a direct question about an app
    the snapshot didn't carry — "I'm connected to Calendar, Gmail and Drive"
    reads as "Notion is not connected". A named app must get a named answer.
    """
    session = SimpleNamespace(asana_live=False, tool_registry=_registry([]))
    snap = capabilities.snapshot("cedric", "org-synthetic", session)
    answer = capabilities.answer("Can you create a page in Notion?", snap)
    assert "Notion" in answer


def test_a_failed_registry_build_never_denies_a_connection(monkeypatch):
    """`ok=False` means the catalog could not be READ — not that nothing is
    connected. Speaking a denial from a failed read is how the live answer
    flip-flopped between "connected to Asana" and "no access at all"."""
    monkeypatch.setattr(tool_registry, "assemble", lambda *_a, **_k: None)
    session = SimpleNamespace(asana_live=False, tool_registry=None)
    snap = capabilities.snapshot("cedric", "org-synthetic", session)
    assert snap["ok"] is False
    answer = capabilities.answer("Is Notion connected?", snap)
    assert "isn't connected" not in answer
    assert "No workspace apps are connected" not in answer


def _catalog_entry(slug: str, deterministic: list[str]) -> dict:
    """One app_policy.catalog row, shaped as the funnel returns it."""
    return {
        "slug": slug,
        "label": slug.title(),
        "connected": True,
        "sources": ["pipedream"],
        "deterministic_types": list(deterministic),
        "api_hosts": [f"api.{slug}.com"],
        "guides": [],
        "can_read": True,
        "can_write": True,
        "requires_approval": True,
        "adapter_required": False,
        "risk": "high",
        "limit": "",
        "note": "",
    }


def test_registry_apps_come_from_the_org_connected_catalog(monkeypatch):
    """assemble() must offer what the ORG actually connected, through the ONE
    policy funnel (app_policy.catalog) — not a capability row that exists only
    once someone has toggled something, and not a second catalog of its own."""
    monkeypatch.setattr(settings, "pipedream_executor", True)
    monkeypatch.setattr(pipedream_executor, "enabled", lambda: True)
    monkeypatch.setattr(
        app_policy,
        "catalog",
        lambda _org: [
            _catalog_entry("notion", ["notion.create_page"]),
            _catalog_entry("github", []),
        ],
    )
    # No explicit per-avatar switches at all — the untouched default.
    monkeypatch.setattr(store, "get_avatar_capabilities", lambda *_a, **_k: {})
    # The plan-gated pre-built catalog must never be required.
    monkeypatch.setattr(
        pipedream_client, "list_actions",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("not on your plan")),
    )
    reg = tool_registry.assemble("org-synthetic", SimpleNamespace(id="cedric"))
    assert [a["slug"] for a in reg["pd_apps"]] == ["notion", "github"]
    assert reg["pd_org_available"] == []
    # Verbs still arrive: the deterministic type for Notion, the registry's
    # generic fallback for the app that has no adapter.
    assert reg["pd_apps"][0]["actions"] == ["create page"]
    assert reg["pd_apps"][1]["actions"] == ["read data", "create and update records"]


def test_an_explicitly_disabled_app_is_present_but_not_promised(monkeypatch):
    monkeypatch.setattr(settings, "pipedream_executor", True)
    monkeypatch.setattr(pipedream_executor, "enabled", lambda: True)
    monkeypatch.setattr(
        app_policy,
        "catalog",
        lambda _org: [_catalog_entry("notion", ["notion.create_page"])],
    )
    monkeypatch.setattr(
        store, "get_avatar_capabilities", lambda *_a, **_k: {"notion": False}
    )
    monkeypatch.setattr(
        pipedream_client, "list_actions", lambda *_a, **_k: []
    )
    reg = tool_registry.assemble("org-synthetic", SimpleNamespace(id="cedric"))
    assert reg["pd_apps"] == []
    assert reg["pd_org_available"] == ["notion"]
    # …and the brief says so honestly rather than denying the app exists.
    text = tool_registry.brief(
        {"native": [], "pd_apps": [], "pd_org_available": ["notion"], "cedric": {}}
    )
    assert "Notion" in text
    assert "NOT enabled for you" in text


# ── 5. the agent's copy ──────────────────────────────────────────────────
def _prompt_with(registry):
    session = SimpleNamespace(
        meeting_url="https://meet.example/synthetic",
        integration={},
        asana_snapshot="",
        roster=lambda: [],
        tool_registry=registry,
    )
    avatar = SimpleNamespace(
        name="Cedric", persona_prompt="You are Cedric.", voice_agent_language="en"
    )
    payload = voice_agent.build_init_payload(session, avatar)
    return payload["conversation_config_override"]["agent"]["prompt"]["prompt"]


def test_meeting_prompt_carries_the_connected_app_catalog():
    prompt = _prompt_with(
        _registry([{"slug": "notion", "actions": ["create page"]}])
    )
    assert "connected_apps" in prompt
    assert "notion" in prompt


def test_meeting_prompt_forbids_denying_a_connected_app_from_memory():
    prompt = _prompt_with(
        _registry([{"slug": "notion", "actions": ["create page"]}])
    )
    flat = " ".join(prompt.split()).lower()
    assert "connected_apps" in flat
    assert "never say an app is not connected" in flat
    assert "only truth about connections" in flat


def test_meeting_prompt_says_prepare_for_approval_never_already_done():
    prompt = _prompt_with(_registry([]))
    # The prompt is assembled as wrapped lines; compare on the flat text so a
    # rewrap never silently drops a rule from this contract.
    flat = " ".join(prompt.split())
    assert "call queue_action IMMEDIATELY" in flat
    assert "Do NOT call get_available_actions first" in flat
    # The honesty contract: prepared/queued, never executed by him.
    assert "you PREPARE them" in flat
    assert "NEVER say it is done, created, scheduled or sent" in flat
    assert "never that it is scheduled/sent/done" in flat
    assert "run ONCE APPROVED" in flat


def test_static_agent_copy_matches_the_live_override():
    """create_meeting_agent.py is the FALLBACK persona: a failed per-call
    override must degrade to the same honesty, not to the old copy."""
    from scripts import create_meeting_agent

    flat = " ".join(create_meeting_agent.PILOT_RULES.split())
    assert "connected_apps" in flat
    assert "never say an app is not connected" in flat.lower()
    assert "you PREPARE them" in flat
    assert "NEVER say it is already done or created" in flat


def test_agent_name_suffix_contract_is_untouched(monkeypatch):
    """This change must not alter which ElevenLabs agent a run would patch.

    The suffix is the cross-environment safety key: a platform that sets
    ``EL_AGENT_NAME_SUFFIX=" (v2)"`` gets its OWN agents instead of rewriting
    the frozen ones. Nothing here runs the script — this pins the naming.
    """
    from scripts import create_meeting_agent

    monkeypatch.delenv("EL_AGENT_NAME_SUFFIX", raising=False)
    assert create_meeting_agent.agent_name_for("cedric", "Cedric") == (
        "Cedric Meeting Pilot"
    )
    monkeypatch.setenv("EL_AGENT_NAME_SUFFIX", " (v2)")
    assert create_meeting_agent.agent_name_for("cedric", "Cedric") == (
        "Cedric Meeting Pilot (v2)"
    )
