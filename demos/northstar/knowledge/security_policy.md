# Security Policy

> SYNTHETIC DEMO DATA. Fictional policy for the Laura demo only.

## Data handling

The Northstar Platform stores workflow configuration, task metadata, and
status. It does **not** store the customer's production records; those remain in
the customer's own systems. During onboarding, integrations use **sandbox**
credentials only; never production credentials.

## Two-person approval

Any action that **writes** to a customer account requires approval by a second
person before it executes. This "two-person approval for write actions" control
is always on for Enterprise customers.

> Note: this is the *security control* that requires a second approver. The
> mechanics of how a specific write is previewed, approved, and recorded live in
> the *Approval Policy*. Similar words ("approval"), different documents:
> security states the requirement, approval states the procedure.

## Credentials

Credentials are provisioned per system and per environment. Sandbox and
production credentials are never interchangeable. Northstar staff never ask a
customer to share production credentials over email or chat.

## Audit

Every guarded write action produces an audit record: who proposed it, who
approved it, the idempotency key, and the resulting receipt. Audit logging is
always on.

## External sharing

External data sharing is **off** by default and must be explicitly enabled per
customer. The demo environment keeps it off.
