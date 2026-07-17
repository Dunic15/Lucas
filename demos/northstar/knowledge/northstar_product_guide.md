# Northstar Platform — Product Guide

> SYNTHETIC DEMO DATA. Fictional product details for the Laura demo only.

## What the platform does

The Northstar Platform connects to a customer's existing systems and runs
workflows across them. A workflow has a trigger, a set of steps, and a set of
guarded actions that require approval before they execute.

## Onboarding pipeline stages

Every customer implementation is tracked as a pipeline of five stages, in order:

1. **Kickoff** — goals, success plan, and roles are agreed.
2. **Data Integration** — the customer's source systems are connected in a
   sandbox. This is where credentials are provisioned.
3. **Configuration** — workflows, fields, and approvals are set up.
4. **UAT** — the customer validates the configured workflows.
5. **Go-Live** — production cutover.

A stage can be *done*, *blocked*, *pending*, or *not started*. A stage is
**blocked** when it cannot proceed without an input the customer owes — most
commonly sandbox credentials during Data Integration.

## Guarded actions

Some steps write to a customer account (creating a task, sending a
notification, opening a ticket). These are **guarded**: the platform shows an
exact preview, and the action executes only after approval. Guarded actions are
**idempotent** — re-running an approved action does not create a duplicate.

## Follow-up tasks

A **follow-up task** records an action a Northstar team member needs to take on
a customer account (for example, chasing a missing input). Creating a follow-up
task is a guarded, reversible, idempotent action. See *Approval Policy →
Follow-up task creation*.

## What the platform stores

The platform stores workflow configuration, task metadata, and status. It does
not store the customer's production records; those stay in the customer's own
systems. See *Security Policy*.
