# FAQ and Troubleshooting

> SYNTHETIC DEMO DATA. Fictional FAQ for the Laura demo only.

## Why is an onboarding stuck?

The most common cause is a **blocked Data Integration stage** — the customer has
not provisioned sandbox credentials. Data Integration is the most common place
for an onboarding to stall. The remedy is a follow-up task to chase the input.

## What is the difference between the escalation timer and the approval window?

They are two different clocks and are easy to confuse:

- **Escalation timer** (Escalation Policy): a **P1** blocker escalates to the
  CSM within **4 business hours**.
- **Approval window** (Approval Policy): a proposed write action is approved or
  rejected within **1 business day**.

If a question is about *chasing a blocker*, cite the escalation timer. If it is
about *approving a write*, cite the approval window.

## Does creating a follow-up task change customer data?

No. A follow-up task is internal Northstar metadata. It is guarded (previewed
and approved), idempotent (approving twice creates one task), and reversible (a
reset restores the prior state).

## Can I use production credentials to unblock Data Integration?

No. Onboarding uses **sandbox** credentials only (see *Security Policy*). Never
use or request production credentials to work around a blocker.

## Who owns the Acme blocker?

Dana Whitfield (CSM). The Implementation Engineer, Marco Ferris, surfaces
technical blockers to the CSM rather than working around them.

## What is "go-live readiness"?

A score out of five: sandbox connected, field mapping approved, workspace
configured, UAT signed off, go-live runbook approved. Do not cut over below 5 of
5. Acme is currently 1 of 5.
