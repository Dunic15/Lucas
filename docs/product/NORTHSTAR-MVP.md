# Northstar MVP — final integration

The integration branch (`claude/demo-mvp-integration`) that combines the
completed **Browser B1 Visual Eyes**, **Dashboard Control Center** and
**Northstar synthetic company** into one testable Laura MVP. Additive only —
no second action / RAG / browser / planner / demo system. All integration
flags default **false**; with them off, production behaviour is byte-identical.
Cedric PR #41 is untouched. Last reviewed: 2026-07-17.

## Source PRs merged

| Branch | Head | PR |
|---|---|---|
| `claude/browser-b0-visual-eyes` (base) | `b968d7a` | #278 |
| `claude/dashboard-control-center` | `d27b8dc` | #274 |
| `ananth/northstar-demo-company` | `ca1380f` | #276 |

Both merges were `--no-ff` and conflict-free (dashboard touches only
`frontend/dashboard.html`; Northstar adds only `demos/northstar/**`); the
browser/config/org_api/main files are byte-identical to `b968d7a`.

## The MVP journey (all proven key-free)

1–4. Open dashboard → Product Specialist avatar (`laura`) → active meeting →
start the Northstar demo (`POST /org/demo/northstar/start` binds org +
principal + meeting + avatar & overlay version + demo def/version + B1 session
+ allowed domain + clamped budgets).
5. Laura grounds Northstar + Acme through **ContextResolver** (cited).
6–8. The B1 coordinator drives the **northstar** browser provider; the
**visual-only** onboarding target (the amber, 2nd node — text-ambiguous across
all five) is chosen by visual grounding, not text.
9. Deterministic checkpoint narration cites the Northstar source, on the
existing speak queue.
10–17. Laura proposes the follow-up task → **one canonical action**
(route=`browser`); the loop pauses for approval → the Action Center shows the
exact operation/avatar/meeting/session/account/target → **rejection creates
zero tasks** → **approval executes exactly once** (task-0003) with a re-check +
post-action visual verification + receipt → a **replayed approval creates no
duplicate**.
18. The browser session closes cleanly.

## Honest capability matrix (real vs simulated)

| Capability | Status |
|---|---|
| Demo-org provisioning, canonical knowledge ingestion, ContextResolver citations + **cross-org denial** | ✅ proven, key-free |
| Meeting-bound session binding; bounded coordinator; visual-only target by grounding | ✅ proven, key-free |
| Guarded follow-up → canonical action; **reject = 0 writes**; **approve = exactly-once** + re-checks + product write + post-action verification + receipt; **replay = no dup** | ✅ proven, key-free |
| Dashboard state mapping; honestly-disabled pause/resume/takeover | ✅ proven |
| Arbitrary browser writes stay disabled (`BROWSER_ALLOW_WRITES=false`) | ✅ proven |
| **Real remote browser (Browserbase) + real multimodal model** | ❌ **UNPROVEN** — inert without keys; only the credential-gated smoke exercises it |
| Meeting autostart, human takeover, real approved writes to a real external site | ⏸️ deferred |

**No fake-provider result substitutes for the real-pixels gate.**

## Feature flags (all default false)

```
NORTHSTAR_DEMO_ENABLED=false        # the demo router + northstar provider
NORTHSTAR_DEMO_WRITE_ENABLED=false  # ONLY the follow-up task write (arbitrary
                                    # writes stay off via BROWSER_ALLOW_WRITES)
BROWSER_OPERATOR_ENABLED=false      # B0 (needs the control plane)
BROWSER_VISUAL_PLANNER_ENABLED=false# B1 coordinator
BROWSER_ALLOW_WRITES=false          # stays false — arbitrary writes disabled
COMPANY_BRAIN_ENABLED=false         # M1 knowledge ingestion (needs Postgres)
DATA_FOUNDATION_ENABLED=false       # DF0-DF1 (optional; resolver works on M1 alone)
BROWSER_ALLOWED_DOMAINS=127.0.0.1:8971,localhost:8971   # for the demo
```

## Migrations

The durable stack requires a Postgres control plane and migrations **0009–0013**
(`alembic upgrade head`): 0009 canonical actions, 0010 company brain, 0011 org
avatars, 0012 data foundation, 0013 browser sessions. **No new migration** —
the integration adds no tables (demo observations are ephemeral; the guarded
action reuses `queued_actions`).

## Safe enable order

1. `alembic upgrade head` (0009–0013) on a control-plane Postgres.
2. `BROWSER_OPERATOR_ENABLED=true`, `COMPANY_BRAIN_ENABLED=true` (and
   `DATA_FOUNDATION_ENABLED=true` if desired).
3. `BROWSER_VISUAL_PLANNER_ENABLED=true`.
4. `NORTHSTAR_DEMO_ENABLED=true`, set `BROWSER_ALLOWED_DOMAINS` to the demo host.
5. `NORTHSTAR_DEMO_WRITE_ENABLED=true` (enables ONLY the follow-up write).
6. Real pixels (optional, separate): `BROWSER_REAL_PROVIDER_ENABLED=true` +
   Browserbase/model keys — then run the smoke below.

**Rollback order** (reverse): clear `NORTHSTAR_DEMO_WRITE_ENABLED` →
`NORTHSTAR_DEMO_ENABLED` → `BROWSER_VISUAL_PLANNER_ENABLED` →
`BROWSER_OPERATOR_ENABLED`. Each flag-off restores prior behaviour; the
migrations are additive.

## Key-free acceptance (one command)

```
./scripts/run_northstar_mvp_demo.sh
```

Starts the local Northstar product (`:8971`), resets it to seed, and runs the
deterministic acceptance TWICE on embedded Postgres (in-process northstar
provider + fake visual planner + real product write). No Browserbase/model
credentials. Cleans up child processes on exit. See
`backend/tests/test_northstar_mvp_e2e.py`.

## Manual local product test

1. Start the Northstar product: `python -m uvicorn demos.northstar.product:app
   --port 8971`. Visit `http://127.0.0.1:8971/` (home), `/customers/acme-robotics`
   (the amber blocked stage), `/tasks`.
2. Start the Laura backend against a control-plane Postgres with the flags above
   (`alembic upgrade head` first). **Key-free identity:** with no Google OAuth
   client and no `LAURA_API_TOKEN`, action doors are open and the org resolves
   to the Demo org — no login needed for the acceptance path. (The dashboard UI
   cookie door needs a session; the acceptance approves through the canonical
   machine door `POST /org/actions/{id}/approve`, which is the same claim +
   receipt path — no production auth is weakened and no bypass is added.)
3. Open the dashboard (`frontend/dashboard.html` served by the backend). Select
   the Product Specialist (`laura`). Open the demo meeting view.
4. Start the browser presentation: `POST /org/demo/northstar/start` →
   `POST /org/demo/northstar/sessions/{id}/run`.
5. **Reject** the follow-up: in the Action Center, reject the pending
   `create_followup_task` action → `/tasks` shows no `task-0003`.
6. **Reset:** `POST http://127.0.0.1:8971/admin/reset` (or
   `python -m demos.northstar.product.reset`).
7. **Approve** it: approve the action → the product create runs once →
   `/tasks` shows `task-0003` with the confirmation banner.
8. See the **receipt** in the Action Center (canonical action receipt carries
   `product.task_id=task-0003`, `verification=verified`).
9. Close the demo: `POST /dashboard/browser/sessions/{id}/close` (the dashboard
   Stop button), or let the TTL expire.

## Real-pixels smoke (credential-gated)

Preserved from B1: `backend/scripts/browser_b1_smoke.py`. To run against a
Northstar preview you need Browserbase + a multimodal key AND a **publicly
reachable** Northstar preview — do NOT expose the localhost product through an
unreviewed public tunnel. Without credentials or a safe preview:

**REAL PIXELS: UNPROVEN** — never replaced with fake-provider evidence.

## Security review

An adversarial review of the integration diff (5 lenses × attack + verify)
found **no HIGH and no MEDIUM**. The confirmed LOW findings were fixed with
regression tests: the demo-write mint gate is now scoped to
`provider='northstar'` (never opens on any other session even under the write
flag), and `ingest.reset` matches strictly on the `northstar-` provenance
prefix (a real same-org source named e.g. `security-policy` is never deleted).
The narration-sanitization and status-leak candidates were verified **not**
real (org/principal never come from the client; only safe public fields leave).

## Known limitations

- The Northstar product store is a **single-tenant synthetic sandbox** (a
  process-global module with a fixed idempotency key). Concurrent multi-org
  demo runs in one process share that synthetic world; it holds only fictional
  Acme data and can never produce a real or cross-tenant external write. Run
  the demo against a dedicated demo org (the default `provision_demo_org`
  path).
- Real remote-browser + real-model pixels are UNPROVEN (no credentials here).
- Checkpoint narration is deterministic per-checkpoint, not a full autonomous
  narration loop (documented scope for the MVP).
- Meeting autostart, human takeover, and authenticated profiles are deferred.
- The demo runs against embedded/real Postgres; the fully key-free path uses
  embedded Postgres exactly like the existing PG test suites.
