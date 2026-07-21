# Browser B0: evaluation report

**Status:** implemented on `claude/browser-b0` (stacked on DF0-DF1 PR #272).
**Scope source:** `docs/product/LAURA-SABLE-BROWSER-OPERATOR-IMPLEMENTATION.md`
(the committed Sable spec), narrowed to a B0 presentation/operator spike.
**Flags:** `BROWSER_OPERATOR_ENABLED=false` (default),
`BROWSER_REAL_PROVIDER_ENABLED=false` (independent), `BROWSER_ALLOW_WRITES=false`
(B0 is read-only). Last reviewed: 2026-07-17.

## What B0 proves

1. **One provider-independent boundary.** `backend/app/browser/provider.py`
   defines the canonical `BrowserOperator` surface (create/get/observe/
   navigate/click/type/scroll/present/close+revoke). Routes, the meeting loop
   and the dashboard never touch a provider; swapping `fake` ↔ `browserbase`
   is a config change, proven by both registering behind `get_provider()`.
2. **Tenancy & ownership are Laura's, never the provider's.** Every session
   row binds org_id + authenticated principal + avatar key/overlay-version +
   meeting ref + provider + TTL + state + command seq, under FORCE RLS with
   composite-org FKs (migration `0013_browser_sessions`). Provider session
   ids (`provider_ref`) are stored server-side and never appear in any API
   response, fixture, or log; proven by test.
3. **The state machine holds.** creating → ready → presenting → closing →
   closed with terminal failed/expired/revoked; invalid-state commands are
   rejected (409), close/revoke are idempotent, TTL expiry is enforced
   server-side both lazily and by the worker reconcile loop (meter safety:
   three independent guards as per the spec's cost posture).
4. **Presentation is safe.** The watcher path never sees a provider URL or
   credential: `present` mints an opaque `lbt_*` token (sha256-stored,
   short-TTL, org+session-scoped, revocable, auto-revoked on every terminal
   state) and the frontend exchanges it server-side for the CURRENT read-only
   viewer payload. Replay after expiry/revoke/close is denied; another org's
   exchange is denied. The `talk.html` `browser_view` control message renders
   the view as an overlay with the avatar as corner tile (additive message on
   the existing at-most-once queue; flag-off pages byte-identical).
5. **Writes cannot bypass the Action Control Plane.** The deterministic
   policy engine classifies every command against the LIVE observation:
   read-only verbs execute; purchase/send/submit/publish/delete/upload/
   account-creation clicks are *guarded*: rejected outright in B0's
   read-only posture, or (with `BROWSER_ALLOW_WRITES=true`) minted as
   canonical `queued_actions` rows with `execution_route='browser'` (the
   0009 CHECK already admits it), carrying the safe param projection + a
   page-state binding fingerprint. Approval flows through the EXISTING doors
   (org + dashboard), behind the EXISTING exactly-once execution claim and
   receipt channel; ownership/state/avatar tool-allowance are re-checked at
   execution time (a revoked session fails the approval closed). Credential/
   secret/MFA fields are blocked outright and their values never enter
   results or logs.
6. **Deterministic feasibility.** The network-free fake provider supports
   deterministic pages, history, clicks, typing, controlled failures
   (`fail://error|timeout|expire`), session expiry and a fake read-only
   viewer; the full suite runs key-free (26 browser tests: 9 unit + 17
   embedded-Postgres blockers).

## What B0 deliberately does NOT prove

- **No real pixels.** No Playwright, no CDP, no real Browserbase session, no
  live-view iframe from a real provider, no screenshot capture. The
  `BrowserbaseProvider` adapter exists behind the interface but every method
  raises `ProviderUnconfigured` until B1 (flag + keys).
- **No planner.** No OpenAI computer-use integration; commands are explicit
  API calls. The policy engine is the authority either way (B1 keeps it).
- **No real writes.** Even an approved guarded step settles its receipt
  without an external write (`b0_no_write: true`). B0 proves the plane, not
  the click.
- **No meeting-lifecycle autostart** (`browser_runtime.on_session_started`),
  no takeover, no profiles, no domain allowlists (org-configured allowlists
  are B5 per the spec).

## Provider assumptions (Browserbase, unvalidated in B0)

- A session can be created per demo and released on close; TTL enforcement
  exists provider-side as a backstop.
- A live-view URL can be minted server-side per viewer, is embeddable in an
  iframe inside `talk.html` under Recall's 720p ceiling, and dies with the
  session. **This is exactly the spec's open question #1. B1's first gate.**
- Provider session ids are opaque and safe to store server-side (they are
  still never exposed).

## Demo-handoff contracts (stable, for the one-company integrator)

These froze in B0 so the demo integrator + Control Center V2 branch never
reverse-engineer the backend. All are provider-independent and secret-free.

- **`BrowserObservation`** (`contracts.build_observation`): session_id,
  command_sequence, page_version, url, title, viewport, screenshot_ref,
  dom_summary, visible_text, elements, truncated, timestamp. `page_version`
  bumps deterministically when the page fingerprint (url+title+element-ids)
  changes; the basis for visual verification and stale-page detection.
- **`VisualPlanner`** (`planner.VisualPlanner`, `observe → propose one`): a
  proposal carries operation, target, arguments, reason, confidence,
  expected_result, observed_page_version, consequential. **The deterministic
  policy stays authoritative**: the operator re-derives the class and can
  override the planner's `consequential` suggestion; a proposal can never skip
  a check. B0 ships `ScriptedPlanner` (deterministic, groundable; a target
  absent from the live observation yields an `observe` replan, not a blind
  click). The real multimodal planner is B1, behind the same interface.
- **Command result** (`contracts.command_result`): accepted, command_sequence,
  page_version, classification, observation (+observation_ref), action_id
  (when approval required), failure_category (from a fixed enum),
  replanning_permitted, verification. Legacy aliases (ok/reason/seq/class)
  are retained for existing callers.
- **Visual verification** (`contracts.verify_expectation`): a command may
  carry `verify=true, expected={...}`; the operator EXECUTES, RE-OBSERVES, and
  compares; returning `verified` / `not_verified` / `inconclusive`. **The
  planner never declares its own operation successful.** A `not_verified`
  result flips `accepted` to false with `failure_category=verification_failed`.
- **Demo-run metadata** (`contracts.clean_metadata`): a bounded, whitelisted
  label, demo_definition_id/version, demo_run_id, current_checkpoint,
  carried on the session. It is **non-authoritative**: it never drives the
  state machine, policy, or provider (a `state` key in metadata is dropped).
- **Presentation events** (`frontend/fixtures/browser_presentation_events.json`):
  the UI-facing lifecycle vocabulary (starting/ready/observing/planning/
  navigating/explaining/paused/waiting_for_approval/human_takeover/verifying/
  completed/failed/expired/closed), each mapped to the backend signal it
  derives from.
- **Handoff fixtures** (`frontend/fixtures/browser_handoff.json`): deterministic
  payloads for successful three-step navigation, visually grounded target,
  guarded write→approval, rejected approval, approved-exactly-once, stale-page
  replan, provider failure, and visual verification success/failure.

## Real-provider smoke procedure (one manual run, B1 entry)

The single end-to-end smoke that promotes B0→B1. **Do not fabricate its
results**: until it runs with real credentials, latency/reliability/cost stay
"unmeasured" above.

1. **Configure (env/SSM only, never git):** `BROWSER_OPERATOR_ENABLED=true`,
   `BROWSER_REAL_PROVIDER_ENABLED=true`, `BROWSER_PROVIDER=browserbase`,
   `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`, and (for the multimodal
   planner) `OPENAI_API_KEY`.
2. **Implement the B1 provider + planner** behind the frozen interfaces:
   `BrowserbaseProvider.{create,observe,navigate,click,type_text,scroll,
   viewer,close}` over Playwright-CDP with real screenshot capture, and the
   real `VisualPlanner` (screenshot → OpenAI computer-use → one proposal).
3. **Run the scripted smoke** against a PUBLIC demo URL (e.g. a public docs or
   pricing page: NOT the synthetic company site, which B0 does not build):
   1. `POST /org/browser/sessions` → session `ready`.
   2. `POST …/present` → exchange the token → confirm the live-view renders
      in `talk.html` inside a real Recall meeting at the 720p ceiling.
   3. Planner-driven **three navigation steps** with **screenshot perception**
      at each (observe → propose → execute), each `verify=true` with an
      `expected` descriptor → confirm `verification: verified`.
   4. **One guarded operation** (e.g. a submit) with `BROWSER_ALLOW_WRITES=true`
      → confirm it mints a canonical action (does NOT execute inline) →
      approve → confirm exactly-once execution + receipt.
   5. **Post-action visual verification**: re-observe after the guarded step
      and confirm the operator (not the planner) reports the verdict.
4. **Secret audit:** grep the run's logs for the provider session id and the
   live-view URL; both MUST be absent.
5. **Record**: create→ready, navigate→observe, exchange→first-paint latencies;
   session stability over 30 min; and the actual browser-hour cost from the
   invoice. Replace the "Measurements: none" section with these numbers.

## Measurements

**None were possible in B0**: no provider credentials are configured in this
environment, and fabricating latency/reliability/cost numbers is forbidden.
To be measured at B1: session create latency, navigate/observe round-trip,
live-view first-paint, session stability over 30 minutes.

**Cost, estimate only (assumptions stated, no measurement):** Browserbase
lists browser-hours as the billing unit on its public pricing page (order of
$0.10–$0.20/browser-hour on paid tiers as of early 2026, plus plan fees). A
30-minute presented session ≈ 0.5 browser-hour ⇒ **$0.05–$0.10 per session**
in provider cost, before plan minimums; Laura-side compute is negligible
(sessions are worker-reconciled rows). Treat these as bounds to validate at
B1, not quotes: verify the current tier sheet before pricing decisions.

## Known risks

- **Live-view embeddability** (iframe CSP/entitlements at 720p) is unproven -
  the whole reason the spec front-loads B0/B1 gates.
- **Fingerprint tolerance:** the approval binding fingerprint (url + title +
  element-ids) IS enforced at execution; a guarded step whose page changed
  since approval fails closed (`stale_page_binding`), and the target element
  must still be present. With a real dynamic page, the *tolerance* of "same
  page state" (how much benign DOM churn to allow) needs a real DOM diff (B1);
  B0 uses exact-match on the structural fingerprint.
- **`queued_actions` direct insert** (`_index_browser_action`) writes the
  durable row directly rather than through the artifact finalize path;
  contract tests pin the columns, but 0009-schema drift would surface here
  (kept in one function on purpose).
- **Token exchange rate-limiting** is not implemented (tokens are short-TTL
  and hash-looked-up; a brute-force sweep is bounded by 403s but not
  throttled); add a limiter before any public viewer surface (B2).

## B1 entry criteria

1. #272 stack review lands (B0 merges only above it).
2. Browserbase key + project available in SSM (never in git).
3. The real-provider procedure above passes its latency/embeddability gate in
   ONE real Recall meeting.
4. Decision on the operator's deploy shape: in-process adapter (as B0 wires
   it) vs the spec's separate `browser_operator/` service. B0's interface
   supports both; the separate service becomes necessary only when Playwright
   runtime load or the WS relay demand it.

## Recommendation

**GO for B1, read-only.** The contracts, tenancy, state machine, token
security and ACP routing are proven deterministic and key-free; the only
unproven layer is provider pixels + latency, which is precisely B1's scope.
Keep `BROWSER_ALLOW_WRITES=false` until the binding-fingerprint re-check
works against a real DOM (B3 per the spec). RunPod remains non-gating; the
Control Center V2 redesign consumes `frontend/fixtures/browser_states.json`
without owning browser logic.
