# Sable — Live Browser Operator (M5) Implementation Spec

**What this is:** the implementation spec for Laura's browser operator
("Sable"): a separate FastAPI service that drives a cloud browser
(Browserbase first) with Playwright, plans visually with the OpenAI Responses
API computer-use tool, runs deterministic DOM/policy checks before every
action, streams a live view onto Laura's meeting tile, and routes every
consequential click through the canonical Action Control Plane (M0, shipped).

**Status:** design — nothing here is implemented. Depends on M0
(`docs/product/UNIFIED-ACTION-CONTROL-PLANE.md`, migration
`0009_canonical_actions`, whose `execution_route` CHECK already admits
`'browser'`). Roadmap context:
[`LAURA-COMPANY-BRAIN-SKILLS-BROWSER-ROADMAP.md`](LAURA-COMPANY-BRAIN-SKILLS-BROWSER-ROADMAP.md).

---

## 1. Shape

```
┌────────────── Zoom/Meet/Teams ──────────────┐
│  Recall bot camera = iframe of talk.html    │  output_media camera kind=webpage
│  ┌─────────────────────────────────────┐    │  (recall_client.py:305-313)
│  │ talk.html: TalkingHead + <iframe>   │    │
│  │ of the Browserbase Live View  ◄─────┼────┼── browser → Browserbase directly
│  └─────────────────────────────────────┘    │   (App Runner WS limit irrelevant)
└──────────────────────────────────────────────┘
        ▲ SSE /avatar/stream + 2s poll (speak, browser_view, ... controls)
        │
┌───────┴───────┐   bearer + X-Laura-Org-Id    ┌──────────────────────┐
│ Laura backend │ ───────────────────────────► │ browser_operator/    │
│ (App Runner)  │ ◄─────────────────────────── │ (separate FastAPI    │
│ M0 actions,   │   HMAC-signed callbacks      │  service, own deploy)│
│ browser_runtime│                             │ Playwright async ──► Browserbase
└───────────────┘                              │ OpenAI computer-use  │
                                               └──────────────────────┘
```

- **`browser_operator/`** is a new top-level directory: its own FastAPI app,
  its own deploy, its own DB schema. It never imports `backend/app` and never
  sees Laura transcripts. It can live on any HTTP-capable host; unlike the
  Gemini-ears relay it needs no inbound WebSocket (App Runner's 403 on WS
  upgrades constrains *Laura's* host, not this service — and the Live View
  iframe streams browser→Browserbase directly, bypassing both backends).
- **BrowserProvider** is an interface; `BrowserbaseProvider` is the first
  implementation (create session → CDP `connect_url` for Playwright, live-view
  URL minting, persisted contexts/profiles, session release). A future
  `LocalProvider` (headed Chromium) slots in for dev.
- **Playwright async Python** drives the provider session: navigation, DOM
  reads, screenshots, and the small executable action vocabulary (§8).
- **Planner** = OpenAI Responses API `computer_use` tool: screenshot +
  distilled goal in, one suggested action out. The planner *suggests*; the
  deterministic policy engine (§8) decides. Model output is never executed
  unchecked.
- **Laura side** = one new module `backend/app/browser_runtime.py` (lifecycle,
  copied from `gpu_runtime.py`), a thin client for the operator API, one new
  control-message type into `talk.html`, and canonical Actions for guarded
  steps. No new speak path.

## 2. Non-negotiables inherited from Laura

| Rule | Grounding |
|---|---|
| The Live View must NEVER reuse the meeting's `conversation_id` delivery channel. `talk.html` drains ONE at-most-once queue via SSE + 2 s poll (`frontend/talk.html:694-712`); a second consumer on that id would steal `speak` messages. The browser view arrives as an **additive control message on the same queue** (one consumer, new message type), and any operator→page data beyond that goes browser→Browserbase inside the iframe | top hard constraint |
| New page behaviour = additive `{type: ...}` message via `main._send_avatar_control` (`backend/app/main.py:3991`) + a new branch in `handleBackendMessage` (`frontend/talk.html:673-689`); pages that don't know the type ignore it. Precedent: `raise_hand`/`lower_hand` (`main.py:4004-4042`) | shipped pattern |
| Iframing is legal: `security.py` deliberately sets no `X-Frame-Options`/`frame-ancestors` because Recall iframes the avatar pages (`backend/app/security.py:20-26`); an iframe *inside* `talk.html` is unaffected by Laura headers (Browserbase live-view embedding is provider-side) | shipped |
| Per-meeting lifecycle: start fire-and-forget after `create_bot` succeeds (`main.py:2276-2277`), end only in the leave-verified finalize branch (`main.py:2997-2998`), never between `leave_call` and the usage-row close — meter safety | `gpu_runtime.py` pattern |
| Every write is a canonical Action, `route='browser'`: durable decision + execution-claim CAS (`outbox_pg.claim_action_execution`), params door, `GET /org/actions/{id}` | M0, shipped |
| New Laura tables: FORCE RLS + `tenant_isolation` on `NULLIF(current_setting('app.current_org', true),'')::uuid` TO `laura_app`, REVOKE-then-GRANT exactly SELECT/INSERT/UPDATE, raw-SQL Alembic at the then-current head (`0010_company_brain` is claimed by M1, so `0011` at the earliest), engine via `control_plane._get_engine()`, workers = lifespan asyncio loop + `FOR UPDATE SKIP LOCKED` leases (`outbox_pg.claim_due`, `outbox_pg.py:1210-1290`) | conventions |
| Key-free demo: `BROWSER_OPERATOR_ENABLED=false` default; with no Browserbase/OpenAI keys every entry point no-ops cleanly and the demo is byte-identical | hard constraint |
| Latency: nothing on the transcript→speak path. Operator calls happen at session start/finalize, in workers, or in fire-and-forget tasks; narration lines are ordinary speaks through the existing queue | hard constraint |
| Transcripts are PII: the operator and the computer-use model receive **bounded distilled goals only**, never transcript text | hard constraint |

## 3. Slices B0–B5 (each one PR-sized)

### B0 — Presentation spike (no service)

Prove the pixel path before writing any service code.

- Manually create a Browserbase session; hand its live-view URL to a dev-only
  Laura endpoint that calls `_send_avatar_control(session, {"type":
  "browser_view", "url": ...})`.
- `talk.html`: new `handleBackendMessage` branches — `browser_view` shows an
  `<iframe>` overlay (avatar shrinks to a corner tile), `browser_view_hide`
  removes it. CSS only; TalkingHead keeps rendering (CDN importmaps untouched).
- **Gate:** in a real Recall meeting the bot camera shows a scrolling live
  page + the avatar corner tile at Recall's 720p ceiling and remains legible;
  speak/stop behaviour unchanged; with the flag off, `talk.html` is
  byte-identical in behaviour.

### B1 — Read-only operator service

- `browser_operator/` FastAPI app: `POST /v1/sessions`, read-only tasks
  (`navigate`, `read`, `find`, `screenshot`), `GET` session, `close`. Machine
  auth (§4). `BrowserbaseProvider` + Playwright async; planner integrated for
  read/navigate suggestions only — the executable action set contains no
  writes yet.
- Own Postgres (sessions/tasks/steps tables, §6); no Laura wiring.
- **Gate:** black-box test drives "open site X, find Y, return text +
  screenshot" through the API; killing the service mid-task leaves a durable,
  resumable/expirable session row; zero-key boot serves 503
  `browser_operator_unconfigured` on every route.

### B2 — Laura lifecycle + presentation bridge

- `backend/app/browser_runtime.py`, a structural copy of `gpu_runtime.py`:
  `on_session_started(...)` called fire-and-forget next to
  `gpu_runtime.on_session_started()` (`main.py:2276-2277`) — creates one
  operator session per meeting (threadpool, never blocks the join);
  `on_session_ended(...)` called only in the leave-verified finalize branch
  (`main.py:2997-2998`) — closes it. Best-effort: operator failure never
  touches the meeting.
- Durable mapping row (`browser_meeting_sessions`, §6) written on create.
- Viewer URL fetched server-side from `GET /v1/sessions/{id}/viewer` and
  pushed via `browser_view`; never logged, never stored in the artifact.
- Orphan reconcile loop in the lifespan (the `/check-sessions` discipline):
  operator sessions whose meeting row is finalized get closed; sessions past
  `BROWSER_DEFAULT_TIMEOUT_SECONDS` expire server-side regardless.
- **Gate:** meeting start → live view appears; meeting end (leave-verified)
  → operator session closed within the reconcile interval; a Laura crash
  never leaves a Browserbase session running past its timeout; flags off /
  keys absent → byte-identical meeting.

### B3 — Intent + narration

- A live "look it up in the browser" intent (same detection tier as existing
  tool intents) produces a **distilled goal**: bounded string (≤ 300 chars) +
  optional start URL, built from the typed request — never raw transcript.
  Posted as a task off the live path (fire-and-forget task creation, like
  `_post_hand_chat`, `main.py:4021-4033`).
- Operator emits narration callbacks ("Opening the pricing page…"); Laura
  turns them into ordinary speaks through the existing queue — subject to the
  same cooldown/turn-taking as any other speech, so narration never talks over
  humans.
- **Gate:** ask in-meeting → view + narrated read-only browsing; transcript
  text provably absent from operator request logs (test asserts the goal
  string, not the utterance, crosses the wire).

### B4 — Approvals via the canonical Action plane

- The policy engine (§8) classifies each planned step. Guarded steps stop in
  `awaiting_approval`; the operator calls back to Laura, which mints a
  canonical Action: `execution_route='browser'`, `typed_json` = the safe param
  projection, `risk` from the action class, `params_schema_json` for the
  dashboard card. Same doors as every action (`/dashboard/actions/{id}/approve`,
  `/org/actions/{id}/approve`).
- On approve: decision recorded first, execution claim CAS'd
  (`claim_action_execution`), then Laura calls
  `POST /v1/sessions/{sid}/steps/{step_id}/approved` with the approval binding
  (§8). The operator re-verifies the binding against the *current* page state
  before executing; mismatch → `blocked` + a fresh `awaiting_approval` step.
- Outcome flows back as `receipt_json` (`{kind:"browser", ref:<step>,
  route:"browser"}`) + `done`/`failed`/`execution_unknown` via the existing
  status door.
- **Gate:** a submit-class step executes exactly once under double approval
  (reuse the M0 race test shape); a page changed between approval and
  execution is re-approved, not executed; rejection lands `blocked` with a
  durable trail.

### B5 — Authenticated profiles, takeover, retention

- **Profiles:** Browserbase persisted contexts, one per (org, profile name);
  logins are performed by a human via takeover, never by the model (no
  credential typing by the planner, ever). Profile ids live in the operator
  DB, org-scoped; Laura references them by name only.
- **Takeover:** dashboard button → session state `takeover`; operator stops
  planning/executing, live view becomes interactive for the human
  (Browserbase live view supports control), release returns to `paused`.
- **Retention:** step screenshots/artifacts to `BROWSER_ARTIFACT_BUCKET`
  under `org/{org_id}/...` with the org's existing retention contract
  (`orgs.retention_days`, migration 0007 precedent); viewer URLs and provider
  session ids excluded from artifacts and logs everywhere.
- **Gate:** an authenticated flow (e.g. internal tool) runs on a profile the
  human logged into; takeover/release round-trips; artifacts expire on
  schedule; secrets audit (grep logs for viewer/provider ids) is clean.

## 4. API contract (operator service, machine-authenticated)

**Auth:** `Authorization: Bearer <BROWSER_OPERATOR_TOKEN>` +
`X-Laura-Org-Id: <uuid>` on every call. The org header scopes the request; a
resource owned by another org answers 404, mirroring Laura's own machine gate
(`org_api.py:314-317` treats the header as a cross-check, mismatch = 404).
Operator→Laura callbacks are HMAC-SHA256 signed with
`BROWSER_OPERATOR_CALLBACK_SECRET` (body signature header + timestamp, 5-min
replay window).

**Idempotency:** every mutating call carries a client-minted `command_id`
(UUID). The operator stores `(org_id, command_id) → result` and answers
replays with the first result — Laura's fire-and-forget + retry callers stay
safe. Approved-step execution is additionally bound by the M0 execution claim
on Laura's side, so even a lost/replayed approve cannot double-execute.

| Endpoint | Purpose |
|---|---|
| `POST /v1/sessions` | create a session: `{command_id, profile?, ttl_seconds?, meeting_ref?}` → `{session_id, state}` |
| `GET /v1/sessions/{id}` | state + current task/step summaries (no viewer URL here) |
| `GET /v1/sessions/{id}/viewer` | mint/refresh the live-view URL: `{viewer_url, expires_at}` — the only route that ever returns it |
| `POST /v1/sessions/{id}/tasks` | `{command_id, goal, start_url?, max_steps?, allow_write?: false}` → `{task_id}`; goal is the bounded distilled string |
| `POST /v1/sessions/{id}/pause` / `resume` | freeze/unfreeze planning + execution (`{command_id}`) |
| `POST /v1/sessions/{id}/cancel-task` | `{command_id, task_id}` — stop cleanly after the in-flight step settles |
| `POST /v1/sessions/{id}/close` | `{command_id}` — release the provider session; terminal |
| `POST /v1/sessions/{id}/steps/{step_id}/approved` | `{command_id, action_id, binding}` — execute one guarded step iff the binding still matches (§8) |
| `POST /v1/sessions/{id}/steps/{step_id}/rejected` | `{command_id, action_id, reason?}` → step `blocked` |

**Callbacks (operator → Laura, signed):** `session.state`, `task.state`,
`step.awaiting_approval` (carries the proposed action class + safe param
projection + binding fingerprint), `step.result`, `narration` (`{text}` —
short, human-written-style lines). Laura's receiving route verifies the HMAC,
then updates the mapping row / mints the Action / queues the speak.

## 5. State machines

**Session** — `creating → ready → running ⇄ waiting_for_approval`,
`running ⇄ paused`, `paused → takeover → paused`, any non-terminal →
`closing → closed`, plus terminal `failed` and `expired`.

| State | Meaning | Enters via |
|---|---|---|
| `creating` | provider session being provisioned | `POST /v1/sessions` |
| `ready` | browser up, no active task | provisioning done / task finished |
| `running` | a task's steps are planning/executing | task posted / resume |
| `waiting_for_approval` | ≥1 step `awaiting_approval`, nothing else runnable | policy engine |
| `paused` | frozen by operator command | `pause` / takeover release |
| `takeover` | a human drives via the live view; planner disabled | dashboard |
| `closing` | provider release in flight | `close` / finalize / reconcile |
| `closed` | terminal, released | — |
| `failed` | terminal, unrecoverable provider/browser error | any |
| `expired` | terminal, TTL (`BROWSER_DEFAULT_TIMEOUT_SECONDS`) hit server-side | watchdog |

**Step** — `planned → policy_checked → (executing | awaiting_approval →
executing | blocked) → observed → completed`, with `execution_unknown` as the
honest crash outcome.

| State | Meaning |
|---|---|
| `planned` | planner suggested an action (screenshot + goal → suggestion) |
| `policy_checked` | deterministic DOM/policy checks passed; class decided (auto vs guarded) |
| `awaiting_approval` | guarded: canonical Action minted on Laura, card pending |
| `executing` | Playwright performing the action (lease held, like M0 `executing`) |
| `observed` | post-action observation captured (DOM/screenshot diff) |
| `completed` | step settled with a result |
| `blocked` | rejected, binding mismatch, policy refusal, or `BROWSER_MAX_STEPS` hit |
| `execution_unknown` | crash/timeout mid-execution; the click may have landed. NEVER blind-retried — reconcile by observation (reload, read state, compare), the same rule M0 applies to stale `executing` leases |

`execution_unknown` on a guarded step propagates to the canonical Action as
`failed` with `receipt_json.kind="browser_unknown"` and a dashboard prompt to
verify by reading — mirroring the M0 doc's "the external write may have
happened" rule.

## 6. Data model

**Operator service (its own Postgres, same tenancy discipline):**
`sessions (org_id, session_id, provider, provider_ref, profile, state,
ttl_at, created_at)`, `tasks (org_id, session_id, task_id, goal, state,
max_steps)`, `steps (org_id, task_id, step_id, seq, action_class, state,
binding_fingerprint, safe_params jsonb, result jsonb)`,
`commands (org_id, command_id, result jsonb)` for idempotency. `provider_ref`
and viewer URLs never appear in logs or API list responses.

**Laura side (raw-SQL migration at the then-current head — `0011`+, since
`0010_company_brain` is claimed by M1 — 0006/0009 discipline):**

```sql
CREATE TABLE public.browser_meeting_sessions (
  org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
  bot_id text NOT NULL,
  operator_session_id text NOT NULL,
  state text NOT NULL DEFAULT 'creating',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, bot_id)
);
-- FORCE RLS + tenant_isolation policy on
-- NULLIF(current_setting('app.current_org', true),'')::uuid TO laura_app;
-- REVOKE ALL then GRANT SELECT, INSERT, UPDATE TO laura_app (no DELETE).
```

Guarded browser steps need **no new action table**: they are rows in
`queued_actions` with `execution_route='browser'` (already admitted by the
0009 CHECK), `typed_json` carrying the safe param projection, and the binding
fingerprint inside `permission_json`. The reconcile worker is a lifespan
asyncio loop claiming stale mapping rows with `FOR UPDATE SKIP LOCKED`
(pattern: `outbox_pg.claim_due`).

## 7. Configuration

**Laura (`backend/app/config.py`, pydantic-settings — lowercase snake_case
fields, UPPER_SNAKE env):** `browser_operator_enabled: bool = False`,
`browser_provider: str = "browserbase"`, `browser_operator_url: str = ""`,
`browser_operator_token: str = ""`, `browser_operator_callback_secret: str = ""`,
`browser_default_timeout_seconds: int = 1800`, `browser_max_steps: int = 30`.

**Operator service env:** `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`,
`OPENAI_API_KEY`, `BROWSER_ARTIFACT_BUCKET`, plus the shared
`BROWSER_OPERATOR_TOKEN` / `BROWSER_OPERATOR_CALLBACK_SECRET`.

`.env.example` addition (box-drawing divider, lowercase booleans, no inline
comments on empty values — matching the existing file):

```
# ─────────────────── Browser operator (Sable) — optional ───────────────────
# Laura performs watchable browser work in meetings via a separate
# browser_operator/ service. Default OFF; with no keys everything no-ops and
# the demo is unchanged. Guarded clicks go through the same approval plane as
# every other action (docs/product/LAURA-SABLE-BROWSER-OPERATOR-IMPLEMENTATION.md).
BROWSER_OPERATOR_ENABLED=false
BROWSER_PROVIDER=browserbase
BROWSER_OPERATOR_URL=
BROWSER_OPERATOR_TOKEN=
BROWSER_OPERATOR_CALLBACK_SECRET=
BROWSER_DEFAULT_TIMEOUT_SECONDS=1800
BROWSER_MAX_STEPS=30
```

**Key-free / degraded matrix (must hold at every slice):**

| Missing | Behaviour |
|---|---|
| `BROWSER_OPERATOR_ENABLED=false` (default) | zero calls, zero control messages; meeting byte-identical |
| Flag on, `BROWSER_OPERATOR_URL/TOKEN` empty | `browser_runtime` no-ops with one boot log line; never errors mid-meeting |
| Operator up, `BROWSERBASE_API_KEY` absent | `POST /v1/sessions` → 503 `browser_operator_unconfigured`; Laura treats as best-effort failure (like a `gpu_runtime` boto3 failure — meeting unaffected) |
| `OPENAI_API_KEY` absent | sessions/read tasks with explicit URLs still work (deterministic navigation); planner-dependent tasks fail fast with `planner_unconfigured` |

## 8. Security & policy engine

The policy engine is deterministic code between the planner and Playwright.
Order per step: plan → DOM verification → classification → (approval) →
execute → observe.

**Deterministic pre-action checks (all must pass):**
- Target element exists in the live DOM and matches the planner's claim
  (role/tag/accessible name within tolerance); coordinates-only clicks with no
  resolvable element are refused.
- Page domain ∈ the task's allowlist. Allowlists are org-configured
  (dashboard, org-scoped rows); default posture: read-only browsing on public
  pages is allowed, **any write class requires the domain to be explicitly
  allowlisted**. Navigation outside the allowlist on an authenticated profile
  → `blocked`.
- No input classified as a credential field (`type=password`, autocomplete
  `current-password`/`one-time-code`, etc.) is ever typed into by the planner
  path — logins are human-only via takeover (B5).
- Step budget: `BROWSER_MAX_STEPS` per task, hard stop → `blocked`.

**Action classification:**

| Class | Examples | Treatment |
|---|---|---|
| auto | navigate, scroll, read, find, screenshot, focus, non-submit typing into non-credential fields | execute after checks |
| **guarded → canonical Action, approval required** | form submit; send (message/mail); purchase/payment; publish/post; delete/archive; any external communication; file upload; account creation | `awaiting_approval` |
| **blocked outright** | extracting passwords/secrets/tokens from pages or storage; anything that reads or bypasses MFA (OTP fields, recovery codes); downloading files or executables; changing security/recovery settings | `blocked`, logged (distilled), surfaced |

**Approval binding.** A guarded step's approval is bound to the tuple
`(skill_run_id?, task_id, step_id, page_state_fingerprint, domain,
action_type, safe_param_projection)`. `page_state_fingerprint` = hash of URL
+ normalised target-element subtree + form-field values (redacted to
shapes for sensitive fields). Laura stores the binding in the canonical
Action's `permission_json`; the approved-step call carries it back; the
operator recomputes against the live page and executes **only on exact
match** — any state change re-enters `awaiting_approval` with a fresh Action.
The safe param projection (what the approver actually saw: recipient, amount,
visible text — never full page content) is what lands in `typed_json` and the
dashboard card.

**Model-input rule.** The computer-use model receives: current screenshot,
distilled goal (bounded), step history summaries, allowed-action vocabulary.
It never receives transcript text, meeting metadata beyond the goal, viewer
URLs, credentials, or org document dumps. Page content is untrusted input —
an instruction on a web page ("click here to confirm") is a planner
*suggestion source* at most; it can never widen the action class or skip a
check (prompt-injection posture: the policy engine, not the model, is the
authority).

**Logging rule.** Neither service ever logs viewer URLs, provider session
ids, page credentials, or raw page content. Step logs are distilled
one-liners (the `logs_json` discipline, bounded, append-only). Laura's
existing guard hook covers transcript prints; browser logs get the same
review bar.

## 9. Latency & meter safety

- **Live path untouched:** intent capture reuses the existing detection tier;
  task creation and every operator HTTP call are fire-and-forget/threadpool
  off the transcript→speak path. Narration enters the normal speak queue and
  obeys existing turn-taking — Sable never adds a synchronous hop to
  answering.
- **Second meter:** a Browserbase session bills like the GPU box — so the
  lifecycle is meeting-bound (B2), TTL-bounded server-side
  (`BROWSER_DEFAULT_TIMEOUT_SECONDS=1800` default), reconciled from Laura
  (orphan loop), and self-expiring on the operator even if Laura dies — three
  independent guards, exactly the `gpu_runtime` cost posture (its docstring:
  backend hook + boot TTL + idle watchdog).
- **Close ordering:** `on_session_ended` runs only in the leave-verified
  finalize branch, after the usage row is closed (`main.py:2997-2998`
  ordering) — a browser-close failure can never delay or mask the meeting
  meter shutdown.

## 10. Open questions

1. Browserbase live-view URL lifetime and embed constraints at Recall's 720p
   ceiling — B0 exists to answer this before any service code.
2. Whether narration should also render as meeting-chat lines (the
   `send_chat_message` path used by hand-raise) for rooms where she should
   stay quiet — likely a per-avatar toggle (M2 overlay).
3. Multi-tab / popup policy — v1: single tab, popups auto-dismissed and
   logged; revisit after B3 telemetry.
4. Where the operator deploys (Fly/Runpod both fit the "WS-capable spare
   host" precedent from the ears relay) — decision owed at B1.
