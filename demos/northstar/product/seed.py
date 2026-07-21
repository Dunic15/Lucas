"""Deterministic seed for the Northstar demo product; the single source of
truth for every fact, id, url, and fixture in this environment.

Everything here is FICTIONAL. There is no real SFF / customer / employee /
credential data anywhere in this package (a test enforces it). Nothing here
reads the clock or a random source: `DEMO_NOW` is a frozen timestamp so the
product, its tests, and the demo manifest all agree byte-for-byte on every run.

Import surface:
    seed_state()  -> a fresh, deep-copied state dict (store.reset uses this)
    CONSTANTS      -> stable ids / urls / version referenced by the manifest+tests
"""
from __future__ import annotations

import copy
from typing import Any

# ── frozen identifiers (the manifest and tests import these) ───────────────
COMPANY_ID = "northstar"
DEMO_VERSION = "1.0.0"
CUSTOMER_ID = "acme-robotics"
AVATAR = "laura"

# A frozen wall-clock. Created records are stamped with this so a create is
# deterministic across runs (no time.time(), no uuid; see store.new_task_id).
DEMO_NOW = "2026-07-20T09:00:00Z"

# The one guarded operation the demo exercises. The idempotency key is STABLE
# (derived from the customer + a fixed slug), which is what makes repeated
# approved execution converge on exactly one task.
FOLLOWUP_IDEMPOTENCY_KEY = "acme-robotics:followup:data-integration-blocker"
FOLLOWUP_TITLE = "Follow up with Acme Robotics on Data Integration blocker"
FOLLOWUP_NOTE = (
    "Ask the Acme operations team to provision the WMS sandbox API "
    "credentials so Data Integration can proceed."
)
FOLLOWUP_ASSIGNEE = "Dana Whitfield"

# ── onboarding pipeline: order is meaningful (left→right in the diagram) ────
# `visual` carries the signal the accessible layer deliberately withholds:
# the stage name is NOT rendered as node text and every node shares the same
# aria-label, so a reader must use fill+position to pick the blocked stage.
ONBOARDING_STAGES = [
    {"id": "stage-kickoff", "slug": "kickoff", "name": "Kickoff",
     "status": "done", "visual": {"fill": "#2f9e5b", "order": 0}},
    {"id": "stage-integration", "slug": "integration", "name": "Data Integration",
     "status": "blocked", "visual": {"fill": "#e0a23b", "order": 1}},
    {"id": "stage-config", "slug": "config", "name": "Configuration",
     "status": "pending", "visual": {"fill": "#8a90a2", "order": 2}},
    {"id": "stage-uat", "slug": "uat", "name": "UAT",
     "status": "pending", "visual": {"fill": "#8a90a2", "order": 3}},
    {"id": "stage-golive", "slug": "golive", "name": "Go-Live",
     "status": "pending", "visual": {"fill": "#8a90a2", "order": 4}},
]
# The one node a correct visual read must land on (the blocked/amber stage).
VISUAL_TARGET_STAGE_ID = "stage-integration"


def _seed() -> dict[str, Any]:
    return {
        "company": {
            "id": COMPANY_ID,
            "name": "Northstar",
            "tagline": "Operations workflow automation for scaling teams.",
            "product": "Northstar Platform",
        },
        "customers": [
            {
                "id": CUSTOMER_ID,
                "name": "Acme Robotics",
                "industry": "Industrial robotics manufacturing",
                "plan": "Enterprise",
                "health": "at_risk",
                "csm": "Dana Whitfield",
                "implementation_engineer": "Marco Ferris",
                "sponsor": "Priya Nandakumar, VP Operations",
                "contract_start": "2026-06-01",
                "target_go_live": "2026-08-15",
                "blocker": (
                    "WMS sandbox API credentials not yet provisioned by Acme; "
                    "Data Integration cannot start."
                ),
            },
        ],
        # Acme onboarding checklist; the ACCESSIBLE counterpart to the diagram.
        "onboarding_checklist": [
            {"id": "chk-kickoff-call", "label": "Kickoff call completed",
             "status": "done"},
            {"id": "chk-success-plan", "label": "Mutual success plan signed",
             "status": "done"},
            {"id": "chk-data-access", "label": "Sandbox data access granted",
             "status": "blocked"},
            {"id": "chk-integration-mapping",
             "label": "Integration field mapping approved", "status": "pending"},
            {"id": "chk-config-workspace", "label": "Workspace configuration",
             "status": "pending"},
            {"id": "chk-uat-signoff", "label": "UAT sign-off", "status": "pending"},
            {"id": "chk-golive-plan", "label": "Go-live runbook approved",
             "status": "pending"},
        ],
        "implementation": {
            "customer_id": CUSTOMER_ID,
            "go_live_readiness": {"complete": 1, "total": 5},
            "milestones": [
                {"id": "impl-integration", "name": "Data Integration",
                 "status": "blocked",
                 "detail": "Waiting on WMS sandbox API credentials from Acme."},
                {"id": "impl-config", "name": "Configuration",
                 "status": "not_started", "detail": "Blocked by Data Integration."},
                {"id": "impl-uat", "name": "User Acceptance Testing",
                 "status": "not_started", "detail": "Scheduled after Configuration."},
                {"id": "impl-golive", "name": "Go-Live",
                 "status": "not_started",
                 "detail": "Target 2026-08-15, at risk given the current blocker."},
            ],
        },
        # Seed tasks; note the demo's follow-up task is deliberately ABSENT so
        # the guarded create has a visible before/after.
        "tasks": [
            {"id": "task-0001", "title": "Send Acme kickoff recap",
             "customer_id": CUSTOMER_ID, "assignee": "Dana Whitfield",
             "status": "done", "created_at": "2026-06-03T09:00:00Z",
             "idempotency_key": "seed:task-0001"},
            {"id": "task-0002", "title": "Confirm success plan signatures",
             "customer_id": CUSTOMER_ID, "assignee": "Dana Whitfield",
             "status": "done", "created_at": "2026-06-05T09:00:00Z",
             "idempotency_key": "seed:task-0002"},
        ],
        # Read-only security posture shown on the Security settings page.
        "security_settings": [
            {"id": "sec-two-person", "label": "Two-person approval for write actions",
             "state": "on"},
            {"id": "sec-audit-log", "label": "Audit log", "state": "on"},
            {"id": "sec-external-sharing", "label": "External data sharing",
             "state": "off"},
            {"id": "sec-sandbox", "label": "Demo sandbox (no external services)",
             "state": "on"},
        ],
        # Monotonic counter for deterministic task ids on create (task-0003…).
        "_next_task_seq": 3,
    }


def seed_state() -> dict[str, Any]:
    """A fresh deep copy — callers mutate freely without touching the seed."""
    return copy.deepcopy(_seed())


CONSTANTS = {
    "company_id": COMPANY_ID,
    "demo_version": DEMO_VERSION,
    "customer_id": CUSTOMER_ID,
    "avatar": AVATAR,
    "demo_now": DEMO_NOW,
    "followup_idempotency_key": FOLLOWUP_IDEMPOTENCY_KEY,
    "visual_target_stage_id": VISUAL_TARGET_STAGE_ID,
}
