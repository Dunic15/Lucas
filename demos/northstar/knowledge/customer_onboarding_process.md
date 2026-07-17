# Customer Onboarding Process

> SYNTHETIC DEMO DATA. Fictional process for the Laura demo only.

## The five onboarding stages

Onboarding is the same five stages for every customer: **Kickoff → Data
Integration → Configuration → UAT → Go-Live**. This is the canonical stage
definition; the product guide and company overview summarise it.

## Stage: Kickoff

Agree goals, sign the mutual success plan, confirm the CSM and Implementation
Engineer, and schedule the integration work. Exit criterion: success plan
signed.

## Stage: Data Integration

Connect the customer's source systems in a **sandbox**. The customer provisions
sandbox API credentials for each system. Exit criterion: sandbox data access
granted and a field mapping approved. **This stage is the most common place for
an onboarding to stall**, because it depends on an input only the customer can
provide.

## Stage: Configuration

Build the workflows, fields, and approval rules. Exit criterion: workspace
configuration complete.

## Stage: UAT

The customer runs user-acceptance testing against the configured workflows.
Exit criterion: UAT sign-off.

## Stage: Go-Live

Cut over to production against the agreed go-live runbook. Exit criterion:
go-live runbook approved and executed.

## Onboarding checklist

Each stage maps to checklist items the CSM tracks: kickoff call, success plan,
sandbox data access, integration field mapping, workspace configuration, UAT
sign-off, and go-live runbook. A blocked checklist item blocks its stage.

## When a stage is blocked

If a stage cannot proceed, the CSM records the blocker and follows the
*Escalation Policy*. The typical remedy is a **follow-up task** to chase the
missing input.
