# Implementation Playbook

> SYNTHETIC DEMO DATA. Fictional playbook for the Laura demo only.

## Purpose

How an Implementation Engineer takes a customer from Kickoff to Go-Live. Pairs
with the *Customer Onboarding Process*, which defines the stages; this doc is
the technical how-to.

## Data Integration: provisioning credentials

Data Integration begins when the customer provisions **sandbox API
credentials** for each source system. The engineer never uses production
credentials during onboarding; only sandbox. If credentials are missing, the
stage is **blocked** and the engineer raises it to the CSM to chase (a
follow-up task), rather than working around it.

Common source systems: a warehouse-management system (WMS), an ERP, and a
ticketing system. For manufacturing customers the WMS is usually the long pole.

## Configuration: field mapping and workflows

Once the sandbox is connected, approve the **integration field mapping**, then
build workflows and approval rules. Every workflow that writes to a customer
account must route its write through a **guarded action**.

## UAT

Hand the configured workflows to the customer's testers. Track defects to
closure. Exit on **UAT sign-off**.

## Go-Live readiness

Go-live readiness is scored out of five: sandbox connected, field mapping
approved, workspace configured, UAT signed off, go-live runbook approved. Do not
schedule production cutover below **5/5**.

## Reversibility

Every write the engineer or CSM makes during onboarding must be reversible and
idempotent, so a demo or a dry run can be repeated safely. Creating a follow-up
task is the canonical example.
