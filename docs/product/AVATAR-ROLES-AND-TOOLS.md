# Avatar roles and tool configuration — Cedric (personal assistant) vs Laura (project manager)

Research-backed mapping (2026-07-31) of what real executive/personal assistants
and project managers do, how they do it, and which tools each avatar should be
configured with. Grounded in the platform's actual tool surface:
`backend/app/actions/action_plane.py` (typed native actions),
`backend/app/brain/tool_registry.py` (per-avatar tool brief), the Cedric
Slack-agent MCP bridge, and the OpenClaw/Pipedream connect-any-app door.

---

## 1. What a personal / executive assistant actually does

Time split: calendar work is ~1/3 of the day; EAs send 40–80 emails/day with
about half following reusable templates; the norm is an action list within 2
hours of meeting end and pre-reads 24–48h ahead.

**Calendar management (highest frequency).** All meeting requests route through
the assistant. They resolve conflicts, insert buffers after intense meetings,
account for travel time, place tentative holds, screen requests without a
stated purpose ("what's the goal, who attends, how long?"), defend focus-time
blocks, coordinate time zones, send invites with agendas attached, run cascade
reschedules when priorities shift, and audit recurring meetings.

**Email/inbox management.** Five-question triage per message (garbage? needs
response? who owns it? hidden deadlines → calendar; next visible step), five
states (Do / Exec-action / Waiting / Read / Archive). They draft replies in the
principal's voice for agreed categories, keep a "Waiting On" list with dated
follow-ups, and convert vague requests into decision packets. The guardrail
that matters for an AI: an explicit operating agreement — categories the
assistant sends alone, categories that are draft-first (anything external),
categories never touched (legal/HR/personal).

**Meeting prep & follow-up.** Briefing doc per meeting (summary, agenda,
attendee context and past interactions, anticipated Q&A, risks); pre-reads out
24–48h ahead; capture every commitment live; owner/due-date action list within
2h; deadline-minus-a-few-days follow-up nudges.

**Gatekeeping.** Screen inbound requests, decline low-value ones politely,
maintain escalation tiers (same-hour VIPs vs daily briefing vs
handle-independently), send the morning day-overview email.

**Travel & expenses.** Book with stored preferences, build itineraries, monitor
disruptions; collect receipts, categorize, submit expense reports.

**Personal errands.** Gifts, reminders for dates, reservations, family
logistics — high delight, low frequency, poor v1 fit.

**Ranked automatable value for a Calendar+Gmail meeting avatar:**
1. Scheduling/rescheduling loop (pure Calendar API + the screening questions)
2. Post-meeting action capture + follow-up emails (the "2-hour list" no human hits)
3. Pre-meeting briefs (attendee context + threads + open items — the frontier
   Microsoft's Copilot Briefing Agent is templating)
4. Inbox triage + draft-on-behalf (with operating-agreement guardrails)
5. Waiting-on tracking ("what are we waiting on?" in under 60 seconds)
6. Morning day-overview brief
7. Focus-time protection (commoditized by Reclaim/Clockwise — table stakes)
8. Travel/expenses (needs Navan/Expensify-class integrations)

**Their real-world tool stack:** Google Calendar / Outlook (core surface),
Gmail with delegation, Calendly, Navan/TripIt (travel), Expensify/Ramp
(expenses), Asana/Todoist (action items), Notion/Docs (SOPs, contact DB,
briefing templates), CRM/contacts, Zapier glue, meeting-AI notetakers.
AI competitors: Motion, Reclaim, Clockwise, Lindy, Copilot Briefing Agent.

---

## 2. What a project manager actually does

Time split: PMs spend up to ~90% of their time communicating and ~54% on
administrative work (status meetings, manual updates, report generation) — the
exact band AI demonstrably compresses.

**Planning/scoping.** Project charter → kickoff; scope statement + WBS; change
control; backlog refinement (~2 sessions/sprint) and capacity math (velocity
minus PTO/ceremonies) before sprint planning.

**Task tracking.** Daily board hygiene: fix stale statuses, verify tickets have
acceptance criteria and estimates, chase overdue owners, log every meeting's
action items into the tracker with owner + due date, verify last meeting's
items closed.

**Status reporting.** The canonical weekly one-pager: overall RAG → 2–3
sentence exec summary → RAG per dimension (schedule/budget/scope/resources/
risk) → done this period → upcoming milestones → top risks → decisions needed
→ budget. Dashboards (burndown, velocity, milestone %) tailored per audience.

**Risk/blocker management.** Living RAID log (every item owned, reviewed at
milestones); impediments noted in standup and personally driven to closure;
escalation with facts-options-recommendation, defined path, lateral-first.

**Meeting facilitation.** Standup (15-min timebox, walk-the-board, parking-lot
deferral, impediment capture), sprint planning, retros (Start/Stop/Continue et
al., max ~3 owned actions carried into the next sprint), reviews, steering
meetings — then distribute minutes highlighting decisions + action items.

**Documentation.** Wiki current: plan, requirements, per-meeting notes, and a
decision log (the institutional memory piece).

**Ranked automatable value for a meeting avatar:**
1. Action-item capture + follow-through into the tracker + chasing — the
   documented gap in every notetaker product ("captured but doesn't move")
2. Status report generation (weekly, formulaic, synthesizable from tracker +
   meeting memory)
3. Standup support (timebox, impediment logging, grounded "status of X" answers)
4. Decision/minutes capture into the wiki (decision log)
5. RAID log upkeep (risks are spoken in meetings but never written down)
6. Blocker chasing + escalation drafting (final escalation stays human)
7. Retro facilitation + carrying the ≤3 actions forward
8. Sprint-planning prep (capacity math, flagging unrefined items)
9. On-demand stakeholder status Q&A grounded in docs/tracker
10. Poor avatar fit: Gantt restructuring, resource rebalancing, budget
    forecasting — judgment-heavy; expose as drafts only.

**Their real-world tool stack:** Jira/Linear/Asana/Monday/ClickUp (trackers),
Confluence/Notion (wiki + decision log), Slack/Teams (nudges, standup
channels), Smartsheet/MS Project (timelines), tracker dashboards, notetakers
(Otter/Fireflies/Fathom/Zoom AI — whose shared weakness, follow-through, is
Laura's differentiation).

---

## 3. Tool configuration — what each avatar should get

Platform surface today: native typed actions `calendar.create_event`,
`email.send`, `asana.*` (avatar-gated via `native_tools`); native reads
`upcoming_meetings`, `company_brain_search`, Drive-folder brief, RAG packs,
meeting memory graph week-brief; expansion via the Cedric Slack-agent MCP
bridge and OpenClaw/Pipedream (~2,800 apps).

### Cedric — personal assistant

| Priority | Tool / capability | Status | Notes |
|---|---|---|---|
| P0 | `google_calendar` (create) | ✅ have | Core PA surface |
| P0 | `gmail_send` | ✅ have | Recaps, follow-ups, on-behalf drafts |
| P0 | **Contacts lookup** (Google People API) | ❌ missing | The 07-31 test died on "email Duccio" because Cedric couldn't resolve an email address. Read-only, auto-approval class. Must also normalize spoken addresses ("x at y dot com"). |
| P0 | **Calendar read incl. past events** | ⚠️ partial | `upcoming_meetings` is forward-only; the test's "what did I do yesterday?" needs a bounded past window in the brief. |
| P1 | `calendar.update_event` / reschedule + free-busy conflict check | ❌ missing | The reschedule cascade is a third of real PA work; today only create exists. |
| P1 | `email.draft` (draft-first, distinct from send) | ❌ missing | Operating-agreement guardrail: external mail lands as a Gmail draft for owner review, not auto-send. |
| P1 | Reminders / lightweight tasks (Google Tasks) + Waiting-On list | ❌ missing | Powers follow-up cadence and "what are we waiting on?" |
| P2 | Morning day-brief + pre-meeting brief generation | partial | Brief machinery exists per-session; productize as a daily send. |
| P2 | Via Pipedream when a customer connects them: Calendly, Expensify/Ramp, Navan/TripIt, Todoist | door exists | Don't build; expose per-org. |
| — | **Not for Cedric:** Asana/Jira tracker writes, RAID/decision log, status reports | | That's Laura's lane; keeps the two demos differentiated. |

Suggested Cedric `tasks:` hints (same mechanism as Laura's):
`schedule_meeting`, `reschedule_meeting`, `send_email` (draft-first when
external), `create_reminder`, `add_waiting_on`.

### Laura — project manager

| Priority | Tool / capability | Status | Notes |
|---|---|---|---|
| P0 | Tracker writes: add `native_tools: [asana]` to `avatars/laura/avatar.yaml` | ❌ one-line config | The executor + `asana.create_task/update_task/add_comment` schemas already exist (Petra-gated). Action-items-into-the-tracker is the #1 PM automatable task. |
| P0 | `email.send` (recap + weekly status report) | ✅ have | Add a status-report template (RAG one-pager) as a process_template. |
| P0 | `calendar.create_event` (follow-ups, ceremonies) | ✅ have | |
| P0 | `company_brain_search` + meeting memory week-brief | ✅ have | Grounded "status of X" answers mid-meeting — the standup superpower. |
| P1 | Decision log + RAID capture | ❌ missing | New typed actions (e.g. `log.decision`, `log.risk`) or Asana-project convention; risks/decisions are spoken in meetings and never written — highest-leverage new capture type. |
| P1 | Owner-chasing nudges (Slack DM via the Cedric bridge, or email fallback) | door exists | The follow-through gap every notetaker has. Approval-gated. |
| P1 | Weekly status-report generation from tracker + memory graph | partial | Week-brief (Slice 1) is the data source; add the RAG-format composer. |
| P2 | Via Pipedream per-org: Jira or Linear (orgs that don't use Asana), Notion/Confluence (minutes + decision log pages) | door exists | |
| P2 | Retro/standup facilitation aids (timebox nudge, parking-lot capture, ≤3 retro actions) | ❌ missing | Persona/mission-level work more than tools. |
| — | **Not for Laura:** travel, expenses, personal reminders, inbox triage, contacts-CRM | | Cedric's lane. |

Suggested additions to Laura's existing `tasks:` hints
(`send_recap_email`, `schedule_followup`, `create_tracker_task` already exist):
`log_decision`, `log_risk`, `chase_owner`, `send_status_report`.

### The differentiation in one line

Both share the meeting-native loop (hear a commitment → queue a typed action →
approve → execute). **Cedric points that loop at one person's life** (calendar,
inbox, contacts, reminders, waiting-on) — his rival is Motion/Reclaim/Lindy.
**Laura points it at a team's delivery** (tracker, status reports, RAID,
decision log, chasing owners) — her rival is the notetakers, and her edge is
exactly their documented weakness: follow-through.
