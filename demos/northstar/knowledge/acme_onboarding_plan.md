# Acme Robotics: Onboarding Plan

> SYNTHETIC DEMO DATA. Fictional plan for the Laura demo only.

## Plan summary

Acme Robotics (Enterprise) is onboarding toward a **2026-08-15** go-live. Two of
seven checklist items are complete; onboarding is currently **blocked** at Data
Integration.

## Stage status

| Stage | Status |
|---|---|
| Kickoff | Done |
| Data Integration | **Blocked** |
| Configuration | Pending |
| UAT | Pending |
| Go-Live | Pending |

## Checklist status

- [x] Kickoff call completed
- [x] Mutual success plan signed
- [ ] **Sandbox data access granted: blocked**
- [ ] Integration field mapping approved
- [ ] Workspace configuration
- [ ] UAT sign-off
- [ ] Go-live runbook approved

## Blocker: WMS sandbox credentials

Data Integration cannot start until Acme provisions **WMS sandbox API
credentials**. This is the single blocker on the plan. Owner to chase: **Dana
Whitfield** (CSM). Severity **P1; go-live at risk** per the *Escalation
Policy*.

## Next action

Create a **follow-up task** for Acme Robotics: chase the WMS sandbox
credentials. This is a guarded, reversible, idempotent action (see *Approval
Policy → Follow-up task creation*).

## Go-live readiness

Readiness is **1 of 5**. It will not move until Data Integration is unblocked.
Do not schedule production cutover below 5 of 5.
