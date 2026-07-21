# Unified Action Control Plane (M0)

One canonical Action object per consequential write, converged on by every
surface (Laura dashboard, Cedric in Slack) and every route (native executor,
Cedric tools, later the browser operator). This document maps the contract to
the code that implements it; it describes what IS built, plus the explicitly
deferred pieces at the end.

## The canonical Action

Durable spine: `queued_actions` in Postgres (FORCE RLS, PK `(org_id,
action_id)`), extended by migration `0009_canonical_actions`. The saved
artifact JSON remains the historical record; richer fields that predate 0009
(proposal, dependencies, approver_user_ids) are merged into the read view.

| Contract field | Where it lives |
|---|---|
| `action_id` | `queued_actions.action_id`: minted at live capture (`ledger.new_action_id`), stable across capture → artifact → Slack card |
| `org_id` | row key; RLS via transaction-local `app.current_org` |
| `origin_avatar` | `queued_actions.origin_avatar` (stamped from `artifact.avatar_id` at index time) |
| `source` | `bot_id` on the row |
| `tool` / `params` | `typed_json` (`{type, args}`); produced by `brain.type_actions` at finalize, editable only through the params door |
| `params_schema` | `params_schema_json`, derived per type from `action_plane.PARAMS_SCHEMAS` |
| `route` | `execution_route` (`native | cedric | browser`), stamped immutably at finalize by `main._stamp_action_routing` |
| `risk` | `risk` (`low | medium | high`), from `action_plane.RISK_BY_TYPE` |
| `permission` | `execution_policy` stamp (always `approval_required` today) + `approver_user_ids` from the artifact |
| `status` | `execution_status` (see lifecycle) |
| `receipt` | `receipt_json` (`{kind, ref, route}`) + human `execution_detail` |
| `logs` | `logs_json`: bounded (50) append-only distilled entries (claim, status, param edits); never transcript content |
| `idempotency_key` | `idempotency_key`, UNIQUE per org where non-empty |

Read it: `GET /org/actions/{id}` (machine) or `GET /dashboard/actions/{id}`
(human) → `org_api._canonical_action_view`.

## Lifecycle

```
'' → needs_details ⇄ proposed → approved → executing → done | failed
                                        ↘ rejected
```

- Vocabulary: `action_plane.ACTION_STATUSES`; DB CHECK in 0009; monotonic
  repaint guard (`outbox_pg._EXECUTION_RANK`) tolerates at-least-once,
  out-of-order webhook delivery. `needs_details` and `proposed` share a rank
  deliberately: finalize may flag an already-proposed card as incomplete and
  an edit moves it back.
- **needs_details**: a typed spec whose REQUIRED fields (per schema) are
  empty. Stamped at index time and enforced at both approve doors (422 with
  `missing_params` + `params_schema`); the fix for "approved but nothing
  executed". Untyped free-text actions are NOT needs_details; they keep the
  Cedric card path unchanged.
- **executing**: an execution claim is held (`execution_lease_until` bounds
  it). A crash mid-execution leaves an observable `executing` row that is
  reconciled, never blindly retried; the external write may have happened.

## Exactly-one-execution (the acceptance gate)

Two independent guards, both required:

1. **Decision record, first-write-wins**: `action_decisions` (PG, 0009) via
   `ledger.record_action_decision`; SQLite `action_approvals` in key-free
   mode. Both approve doors record the decision BEFORE dispatch and answer
   replays/conflicts from the recorded row (`idempotent_replay` / 409).
2. **Execution claim**: `outbox_pg.claim_action_execution`: atomic CAS
   `('', needs_details, proposed, approved) → executing`. Exactly one winner
   across App Runner instances and surfaces; losers report the winner's
   status. Proven under real concurrency in
   `test_canonical_actions_pg.py::test_claim_race_has_exactly_one_winner`.

The execution idempotency key (`action_plane.execution_idempotency_key` →
`exec:{action_id}[:{slot}]`) is stamped on the claim, UNIQUE per org, and
already rides every durable Cedric delivery as the `Idempotency-Key` header.

## Doors

| Door | Auth | Notes |
|---|---|---|
| `POST /org/actions/{id}/approve` | per-org bearer | handshake contract door; decision-first + claim; slot rules; dependency gate |
| `POST /dashboard/actions/{id}/approve` | cookie + same-origin | now records the decision and claims before executing (repeat click = replay, not re-execution) |
| `POST /org/actions/{id}/params`, `POST /dashboard/actions/{id}/params` | as above | the ONLY seam that changes a typed spec (`org_api.apply_param_edits`): schema-validated, refused after a decision or once executing |
| `POST /org/actions/{id}/status` | per-org bearer | inbound provenance; normalizes peer aliases (`executed→done`, `declined→rejected`, …) via `action_plane.normalize_status` |
| `GET /org/actions/{id}`, `GET /dashboard/actions/{id}` | as above | the canonical view |

Dispatch is synchronous by default; `ACTION_DISPATCH_ASYNC=true` makes doors
answer `executing` immediately and settle done/failed from a worker thread.
The claim is active regardless of the flag.

## Events

- `action.requested` (durable outbox, capture time): unchanged.
- `action.status` (single-attempt mirror after native execution); unchanged,
  now carries structured receipts on the row.
- `action.updated` (NEW, single-attempt after a params edit): `{action_id,
  status, params, missing_params}` in the agreed events envelope; 0009 also
  admits it to the durable outbox vocabulary for future durable use.
- Cedric→Laura remains `/org/actions/{id}/status` + `/resolve`; Cedric's
  `executed` state is translated to `done` on Cedric's wire AND aliased on
  Laura's inbound boundary (belt and braces).

## Deferred (explicitly not in this slice)

- **Cedric Slack Edit button/modal**: greenfield on Cedric (no
  `view_submission` handling exists there). The params door + `action.updated`
  event are the agreed substrate; the Slack modal posts to
  `POST /org/actions/{id}/params` with the org bearer.
- **Enforced idempotency column on Cedric's DB**: Cedric today dedupes via
  `seen_events` + `bot_id` uniqueness + application-level `action_id` checks;
  a DB-unique execution key on `meet_proposals` is Cedric-side work.
- ~~Deferred execution of dependency-blocked approvals~~: SHIPPED:
  `backend/app/action_deps.py`. An action reaching terminal `done` releases the
  approvals parked on it; each release runs behind the same
  `claim_action_execution` CAS as the door, so a release racing an approve
  produces one external write. Chains (A→B→C) settle in one bounded sweep; a
  re-entrancy guard turns the nested `set_action_status` into a no-op and the
  outer loop re-scans. Only `done` releases: a `failed`/`rejected` dependency
  leaves dependents parked. Cedric-routed and capability-blocked releases unpark
  but execute nothing, and say so on the provenance channel rather than implying
  a run.
  **The hook is `ledger.set_action_status`, NOT `set_action_decision_result` as
  this doc previously suggested**: the latter is only ever called from the
  approve door, so it never sees a dependency Cedric executed itself and
  reported via `POST /org/actions/{id}/status`; hanging the release there would
  have stranded every dependent of Cedric-completed work.
  Why it mattered before any producer exists: the [M8] gate was live, Cedric's
  relay contract v3 (`hsk_con_ycbd1shbdbh4p5p5cgg2`) hard-stops on `blocked_on`
  because ordering is Laura's to enforce, and nothing released; so the first
  producer to stamp `dependencies` would have made approvals vanish silently
  (approved, never run, no error).
- ~~Reconciliation for stale `executing` leases~~: SHIPPED in the hardening
  slice: `backend/app/action_reconcile.py`, triggered lazily (throttled) from
  the dashboard summary and the canonical GET. Calendar claims are verified
  by reading the calendar (found → `done` with a real receipt; absent →
  `failed` "not created"); unverifiable types settle `failed` with an
  explicit `execution_unknown` receipt after a grace period. Never a blind
  retry. This is the gate that had to land before `ACTION_DISPATCH_ASYNC`
  may be enabled.
