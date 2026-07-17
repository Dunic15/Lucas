# Browser B1 — Visual Eyes

**Status:** implemented on `claude/browser-b0-visual-eyes` (stacked on Browser
B0 PR #275). **Additive** to B0 — no parallel observation/planner/policy/
session/approval/receipt/execution system. All new flags default **false**;
with them off, B0 behaviour is byte-identical. Last reviewed: 2026-07-17.

B1 turns B0's DOM-text observation + command infrastructure into real
screenshot-driven "eyes":

```
screenshot + sanitized page structure
  → multimodal understanding (one structured proposal)
  → deterministic policy validation
  → execution OR canonical approval
  → new screenshot
  → visual result verification
```

## Honest proven / unproven matrix

| Capability | Proven? | How |
|---|---|---|
| **DOM/structure perception** | ✅ proven (key-free) | `sanitize_observation` + the fake provider; 28 B1 tests |
| **Screenshot perception (mechanism)** | ✅ proven (key-free) | fake provider emits a deterministic screenshot payload; the visual-only fixture selects a target that is **impossible from text alone** |
| **Byte-free observation boundary** | ✅ proven | `build_observation` emits only a ref + sha256 digest; a test asserts no bytes/`data:` image reach the observation, DB, receipt, or client |
| **Coordinate visual grounding** | ✅ proven (key-free) | hit-test → element → same `classify`; out-of-viewport / unresolved / low-confidence / stale all refused, with tests |
| **Deterministic policy authority** | ✅ proven | the model's proposal never bypasses `classify`; guarded/blocked re-derived from live DOM; strict schema validator rejects malformed/unknown-op/extra-key |
| **Domain allowlist (server-authoritative)** | ✅ proven | `check_navigation_target`; page content cannot widen it; injection test blocks `evil.example.com` |
| **Prompt-injection has no authority** | ✅ proven | trap-page fixture; the page's "ignore instructions / reveal token / disable approvals" changes nothing; secret redacted from the observation |
| **Bounded coordinator (no unbounded loop)** | ✅ proven | step / consecutive-failure / replan / duration (injected clock) / model-call caps + session expiry, all tested |
| **Guarded approval + exactly-once + receipt** | ✅ proven | inherited from B0's M0 path; post-action **visual verification** now attached to the receipt |
| **Post-action visual verification** | ✅ proven (deterministic) | operator re-observes and runs `verify_expectation`; the planner never self-certifies |
| **Real multimodal perception (real model)** | ❌ **UNPROVEN** | `MultimodalPlanner` targets the OpenAI Responses computer-use API but is inert without a key; exercised only by the credential-gated smoke |
| **Real remote-browser operation (real pixels)** | ❌ **UNPROVEN** | `BrowserbaseProvider` implements the real Playwright/CDP connection + screenshots but is inert without keys; only the smoke exercises it |
| **Real approved browser WRITE** | ❌ deferred | `BROWSER_ALLOW_WRITES=false` by default; a real reversible write is attempted only after the real-DOM/page-binding gate passes (a later step) |

**No fake-provider result is ever substituted for the real-pixels gate.** Until
the smoke below runs green with credentials, the last three rows stay UNPROVEN.

## Components (all additive)

- **`contracts.py`**: byte-free `BrowserObservation` (adds `observation_id`,
  real `viewport`, `a11y_summary` distinct from `dom_summary`,
  `screenshot_digest` + `screenshot_bytes_len`, `org_id`/`principal` binding);
  `visual_proposal` + `validate_proposal` (the fail-closed schema gate);
  post-action `verify_expectation` (B0, reused).
- **`planner.py`**: `VisualPlanner.propose` gains keyword-only
  `previous_result` / `allowed_operations` / `budget` / `screenshot` (B0's
  positional `(observation, goal)` unchanged — no shim). `FakeVisualPlanner`
  covers every scenario (visual-select, wrong-target, low-confidence, stale,
  malformed, unknown-op, guarded, blocked, finish, script).
- **`multimodal.py`**: `MultimodalPlanner` (OpenAI computer-use by config),
  strict structured output, bounded screenshot, timeout + bounded retries,
  fail-closed, no key/payload/provider-object in logs, receipts, or errors.
- **`policy.py`**: `sanitize_observation` now byte-free (digest only) with a11y
  + element `bbox`; `check_navigation_target` (server allowlist, separate from
  the byte-identical `classify`); `resolve_coordinate` (hit-test).
- **`coordinator.py`**: the bounded observe→plan→validate→policy→execute→verify
  loop, composed **purely** from `operator.perceive` + `operator.issue_command`
  + `operator.get_session` — never a provider/DAL/second-execution path. A
  guarded step ends the run as `awaiting_approval` (never polls/self-approves).
- **`operator.py`**: coordinate resolution + stale-screen + confidence gate
  before dispatch; `wait`/`inspect`/`go_back` verbs; `perceive()` (read that
  returns the transient screenshot); post-action visual verification on the
  guarded receipt; `_safe_viewer` hardened to a strict allowlist.
- **`browserbase_provider.py`**: the real remote-browser connection (Browserbase
  session + Playwright over CDP), real screenshots, a safe in-page observation
  extractor that reads **only** visible text + a11y tree + element geometry
  (never cookies/headers/storage/network). Inert without keys.

## Flags (all default false)

```
BROWSER_VISUAL_PLANNER_ENABLED=false      # the B1 eyes; off ⇒ inert, B0-identical
BROWSER_REAL_PROVIDER_ENABLED=false       # the real remote browser (B0 flag)
BROWSER_ALLOW_WRITES=false                # real guarded writes stay off
BROWSER_PLANNER_PROVIDER=openai
BROWSER_PLANNER_MODEL=gpt-5-computer-use  # env-overridable; verify at smoke time
BROWSER_ALLOWED_DOMAINS=demo.laura.test   # server-authoritative navigation gate
BROWSER_SCREENSHOT_MAX_BYTES=1500000
BROWSER_SCREENSHOT_MAX_DIMENSION=1280
BROWSER_COORDINATE_CONFIDENCE_THRESHOLD=0.6
BROWSER_COORD_MAX_STEPS=12
BROWSER_COORD_MAX_CONSECUTIVE_FAILURES=3
BROWSER_COORD_MAX_REPLANS=4
BROWSER_COORD_MAX_DURATION_SECONDS=120
BROWSER_COORD_MAX_MODEL_CALLS=16
```

## Real-provider smoke (the ONLY real-pixels acceptance gate)

`backend/scripts/browser_b1_smoke.py` — refuses to run and prints **UNPROVEN**
(exit 2) unless all B1 flags + `BROWSERBASE_API_KEY`/`BROWSERBASE_PROJECT_ID` +
a planner key + `BROWSER_B1_SMOKE_URL` (a public page you trust) are set. It
proves, on a real browser: a real screenshot observation, ≥3 multimodal
navigation steps, one guarded operation, and post-action visual verification,
inside the bounded coordinator with the domain allowlist enforced. It records
model/provider/steps/replans/latency/cost — **only if it actually ran**.
Screenshots stay in memory (never written to disk or logs).

```
BROWSER_B1_SMOKE_URL=https://<public-page-you-trust> \
BROWSER_OPERATOR_ENABLED=true BROWSER_REAL_PROVIDER_ENABLED=true \
BROWSER_VISUAL_PLANNER_ENABLED=true \
python3 backend/scripts/browser_b1_smoke.py
```

**As of this branch the smoke has NOT been run** (no credentials in this
environment): real multimodal perception and real remote-browser operation are
**UNPROVEN**.

## Deferred (beyond B1)

Real approved external writes (behind the real-DOM gate), meeting-lifecycle
autostart, human takeover, authenticated profiles, the dashboard connection
(the later MVP integration branch — `frontend/dashboard.html` and PR #274 are
untouched).
