# Approval Policy

> SYNTHETIC DEMO DATA. Fictional policy for the Laura demo only.

## What needs approval

Every action that **writes** to a customer account is guarded and needs
approval before it executes: creating a follow-up task, sending a customer
notification, or opening a ticket. Read-only actions (viewing status, reading a
brief) never need approval.

## The approval window

A proposed write action must be **approved or rejected within 1 business day**.

> Note: this 1-business-day window is the *approval* timer. It is not the
> *escalation* timer in the *Escalation Policy* (which escalates a P1 blocker to
> the CSM within 4 business hours). Two different clocks; do not conflate them.

## The procedure

1. **Preview** — the exact record that would be written is shown. No write
   happens at preview.
2. **Decide** — a second person approves or rejects (see *Security Policy →
   Two-person approval*).
3. **Execute** — on approval, the write runs exactly once. On rejection, nothing
   is written.
4. **Receipt** — a receipt records the outcome, the idempotency key, and the
   created record's id.

## Follow-up task creation

Creating a follow-up task is the canonical guarded action:

- It shows an exact **preview** before any write.
- It carries a **stable idempotency key**, so approving twice creates exactly
  one task.
- **Rejection creates zero tasks.**
- It is **reversible**: a reset restores the prior state, so it is safe to
  repeat between demonstrations.

## Idempotency

Idempotency is keyed on a stable key derived from the customer and the action —
not on a timestamp — so retries and double-clicks converge on one record.
