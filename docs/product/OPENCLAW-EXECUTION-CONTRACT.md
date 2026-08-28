# OpenClaw execution contract — approval, dependencies, receipts, replay

**Status:** implemented in [`backend/app/openclaw/`](../../backend/app/openclaw/).
**Off by default and tenant-allowlisted** — an org is active only when
`OPENCLAW_EXPERIMENT_ENABLED=true` **and** its `org_id` is in
`OPENCLAW_EXPERIMENT_ORGS`. With the flag off, Laura's post-meeting behaviour is
byte-identical to today: nothing in this document runs. Last reviewed:
2026-08-09.

This is the contract the runtime enforces **server-side**, on Laura's own state.
The gateway is a language model: it is *told* to execute steps in ascending
order, exactly once, but nothing stops it calling step 3 first, calling step 2
after step 1 failed, or calling the same step twice with a new id. None of the
guarantees below depend on the gateway behaving.

---

## 1. The four guarantees

| Guarantee | Where it is enforced |
|---|---|
| **Approval** — no vendor write before an explicit human Start | [`runtime.approve_action`](../../backend/app/openclaw/runtime.py), [`runtime.start_chat_workflow`](../../backend/app/openclaw/runtime.py) |
| **Dependencies** — a step runs only after every prerequisite is `done` | [`openclaw/dependencies.py`](../../backend/app/openclaw/dependencies.py) + `runtime._dependency_refusal` |
| **Exactly-once** — one vendor write per action, ever | `openclaw_tool_calls` unique index `openclaw_tool_calls_action_once` on `(org_id, run_id, action_id)` |
| **Truthful status** — a run never reports success it did not achieve | `runtime._finish_openresponses_run` |

### Approval

Creating a run **proposes**; it never executes. Actions sit `queued` until a
human approves them — the dashboard Approve button, or the **Start workflow**
click in OpenClaw Chat, which is the single explicit approval for every step
the user was shown.

Only approved actions are handed to the gateway: the tool schema's `action_id`
enum and the plan JSON both contain **only** the approved ids, so an unapproved
sibling is not merely refused — the gateway never learns it exists.

### Dependencies

An action's `depends_on` holds **canonical action ids**. (Chat drafts use 1-based
step numbers; `start_chat_workflow` converts them before the run is written.)

Before any vendor call, the runtime decides from state it owns — the plan from
the stored run payload, the prerequisite status from the action rows:

| Prerequisite state | Decision | Effect |
|---|---|---|
| every prerequisite `done` | **allow** | the action executes |
| a prerequisite still `queued` / `planning` / `running` | **wait** | HTTP **409** `dependency_not_ready`, **no write, no status change** — the step stays claimed and runs later |
| a prerequisite `failed` / `cancelled` / `needs_attention` | **blocked** | the action is settled `needs_attention` with a receipt naming the blocker; **no vendor call** |

A plan that is structurally impossible is refused **fail-closed** before the
gateway ever sees it — unknown dependency, self-dependency, a dependency
declared *after* the action, a cycle, or a duplicate `action_id`. Anything that
transitively depends on a rejected action is rejected too.

When a step ends `failed`/`needs_attention`, its whole downstream chain is
settled immediately rather than waiting for a gateway call that may never come.
Only actions that actually declare the broken step as a prerequisite are
touched — an unrelated sibling is never swept up.

> **Why `needs_attention` and not `cancelled`:** a run whose actions are all
> `done`/`cancelled` is reported as a completed run. Settling a blocked
> dependent `cancelled` would quietly repaint a broken chain as a success.

### Exactly-once

The first tool call recorded for an action wins. A second call — **same
`step_id` or a different one** — replays the recorded result and performs no
vendor write. Concurrent attempts collide on the unique index, so exactly one
executes and the losers replay. A gateway retry after a transport failure
therefore re-reads the receipt instead of re-writing.

Exactly-once outranks the dependency gate: an action with a recorded tool call
already wrote to the vendor, so replaying its receipt is the honest answer even
if the plan has since gone sideways.

---

## 2. Live variables

No values here — set them in `.env` or on the App Runner service. **No secrets
in git.** Env var = UPPER_CASE of the field name in
[`backend/app/core/config.py`](../../backend/app/core/config.py).

### Required

| Variable | Purpose |
|---|---|
| `OPENCLAW_EXPERIMENT_ENABLED` | Master switch. `false` (default) = the whole path is inert. |
| `OPENCLAW_EXPERIMENT_ORGS` | Comma-separated `org_id` allowlist. `*` enables every org — do **not** use it in production. |
| `OPENCLAW_GATEWAY_URL` | OpenResponses base URL. `/v1/responses` is appended when absent. Empty = the run settles `needs_attention` with `gateway_not_configured` and **no fallback executor runs**. |

### Optional

| Variable | Purpose |
|---|---|
| `OPENCLAW_GATEWAY_TOKEN` | Bearer token for the gateway. Omitted = no `Authorization` header. |
| `OPENCLAW_AGENT_ID` | Sent as `x-openclaw-agent-id`. Defaults to a test agent id — set it explicitly for a real gateway. |
| `OPENCLAW_AUTO_RUN` | Legacy flag. It **cannot** bypass approval; leave it off. |
| `OPENCLAW_BROWSER_ENABLED` | Whether the manual browser fallback tool is offered at all (see §5). |

### Connected-app executors (what the actions actually write through)

| Variable | Purpose |
|---|---|
| `NATIVE_EXECUTOR` | Laura's own Google executor (Calendar/Gmail). On by default; still needs a completed `/oauth/google/connect`. |
| `PIPEDREAM_EXECUTOR` | Routes Pipedream-owned families (Asana, Notion, the generic API proxy) through Pipedream Connect. **Off by default** — with it off, Notion-style actions have no executor and settle as failed. |
| `PIPEDREAM_PROJECT_ID`, `PIPEDREAM_CLIENT_ID`, `PIPEDREAM_CLIENT_SECRET` | Pipedream Connect credentials. |
| `PIPEDREAM_ENVIRONMENT` | `development` or `production`. Connected accounts do **not** migrate between environments — an account connected in one is invisible in the other. |

---

## 3. Safe live smoke test

**Use a throwaway workspace, never a customer's.** A real vendor object gets
created; this procedure keeps the blast radius to a scratch page you own.

1. **Prepare a scratch workspace.** A personal Notion workspace with one page,
   named so it is obviously disposable (e.g. `LAURA SMOKE — delete me`). Connect
   **that** account, not a shared one.
2. **Allowlist one org.** `OPENCLAW_EXPERIMENT_ENABLED=true` and
   `OPENCLAW_EXPERIMENT_ORGS=<the scratch org_id>` — one id, never `*`.
3. **Point at the gateway.** Set `OPENCLAW_GATEWAY_URL` (+ token / agent id).
4. **Turn on the executor** the actions need (`PIPEDREAM_EXECUTOR=true` for
   Notion; `NATIVE_EXECUTOR` for Gmail/Calendar).
5. **Ask for a two-step chain in OpenClaw Chat** — e.g. "update the smoke page,
   then create a follow-up page linked to it". Confirm the second step shows a
   dependency on the first.
6. **Stop before starting.** Check the vendor: **nothing** must have changed.
   That is the approval boundary — proposing is not doing.
7. **Click Start workflow.** Watch the run detail: step 1 `running` → `done`
   with a receipt, then step 2 `running` → `done`. Step 2 must never reach
   `running` before step 1 is `done`.
8. **Verify in the vendor**: exactly one updated page and exactly one new
   follow-up page.

### Verifying no duplicate object was created

The point of the exercise. Three independent checks:

1. **In the vendor.** Search the workspace for the follow-up page title. Exactly
   one result. Notion also shows *Created time* — one entry, not two seconds
   apart.
2. **In the run detail.** Each action has exactly one entry in its `tools` list
   and one receipt. A `"replay": true` result means a call was *served from the
   record*, not re-executed — that is the healthy signal, not a warning.
3. **In the tool-call ledger.** One row per action:

   ```sql
   SELECT action_id, COUNT(*) FROM openclaw_tool_calls
   WHERE org_id = :org AND run_id = :run GROUP BY action_id;
   ```

   Every count must be `1`. More than one row for an action means the unique
   index `openclaw_tool_calls_action_once` is missing on that database — stop
   and fix that before running anything else live.

To exercise replay deliberately, re-post the same tool call with a **different**
`step_id`: the response must come back `replay: true` and the vendor must show
no new object.

---

## 4. What the gateway sees (and what it never sees)

The plan sent to OpenClaw carries the meeting summary, decisions, participants,
and the approved typed actions. It carries **`raw_transcript_included: false`
and no transcript field** — transcripts are PII and never leave memory.

The gateway also never receives the action's arguments as authority: it passes
back only `action_id` + `step_id`, and Laura executes the **stored canonical
arguments**. A gateway that invents a recipient, a time, or an API body cannot
make Laura act on it.

Refusals travel back as structured data, never as an exception:

| HTTP | `code` | Meaning |
|---|---|---|
| 401 | — | Capability invalid, expired, or naming a run this org does not own |
| 403 | — | Action not in this run, or tool does not match the canonical action type |
| 409 | `dependency_not_ready` | Called out of order; retry after the prerequisite lands |
| 409 | `dependency_blocked` | A prerequisite can never complete; this action is settled |
| 409 | `invalid_dependency_plan` | The plan is structurally impossible; fail-closed |

A completed call returns HTTP **200** with the *action* status (`done`,
`needs_attention`, …) in the body — action status and HTTP status are
deliberately separate.

---

## 5. Known limitation — the browser fallback is manual

`browser_fallback` does **not** drive a browser. It requires a human with an
authenticated session, and it says so: the action settles `needs_attention` with
"browser fallback requires a manual authenticated browser session". It is
reported truthfully rather than as a completed action — and because
`needs_attention` is a terminal, unmet state, anything depending on that step is
settled unmet too.

Anything that only a real browser can do is therefore **not automatable on this
path today**. Do not demo it as if it were.

---

## 6. Rollback

Per workspace, no deploy:

```
OPENCLAW_EXPERIMENT_ORGS=          # drop the org from the allowlist
```

Or globally:

```
OPENCLAW_EXPERIMENT_ENABLED=false
```

Either way the org's post-meeting actions immediately return to Laura's normal
executors, and in-flight runs stop being started. Already-recorded receipts are
kept — rollback never rewrites history, and because every write is recorded
exactly once, re-enabling later cannot replay a past vendor write.

A run stuck mid-flight can be settled from the dashboard
(`POST /dashboard/openclaw/runs/{run_id}/cancel`), which closes the run and its
actions without touching any vendor.

---

## 7. Tests

- [`backend/tests/test_openclaw_dependencies.py`](../../backend/tests/test_openclaw_dependencies.py)
  — the contract above, with fakes only (no gateway, no vendor, no keys).
- [`backend/tests/test_openclaw_experiment.py`](../../backend/tests/test_openclaw_experiment.py)
  — the surrounding experiment: gating, routing, chat, end-to-end.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  backend/tests/test_openclaw_dependencies.py \
  backend/tests/test_openclaw_experiment.py -q
```
