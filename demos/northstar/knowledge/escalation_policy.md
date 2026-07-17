# Escalation Policy

> SYNTHETIC DEMO DATA. Fictional policy for the Laura demo only.

## Blocker severity and timers

A **blocker** is anything preventing an onboarding stage from proceeding.
Severity sets the escalation timer:

- **P1 — go-live at risk**: escalate to the CSM within **4 business hours** and
  to the CSM's manager within **1 business day**.
- **P2 — stage stalled, go-live not yet at risk**: escalate to the CSM within
  **1 business day**.
- **P3 — minor**: raise at the next weekly review.

> Note: these timers are for *escalation*. They are distinct from the approval
> window in the *Approval Policy*, which governs how quickly a proposed write
> action must be approved or rejected. Do not conflate the two.

## The standard remedy

Most blockers are a missing customer input. The standard remedy is a
**follow-up task** assigned to the CSM, chasing the input. A follow-up task is
guarded, reversible, and idempotent (see *Approval Policy*).

## Worked example

Acme Robotics' Data Integration is blocked because sandbox WMS credentials have
not been provisioned. With a go-live target of 2026-08-15, this is treated as
**P1 — go-live at risk**, so it escalates to the CSM within 4 business hours and
the remedy is a follow-up task to chase the credentials.

## What not to do

Do not work around a blocker by using production credentials, and do not skip a
blocked stage. Record the blocker, escalate on the timer, and chase the input.
