# Laura Surface API — v1 (implemented)

**Status: LIVE contract.** This documents what the code on `main` actually
serves. Laura is the meeting-avatar (senses) half of the **Laura + Cedric
system**: she senses the meeting and hands the distilled artifact + agreed
actions to Cedric (the Slack brain/hands) over this HTTP API — Cedric is the
connected orchestrator (client #1). The API stays open to a second orchestrator,
and Laura can also run standalone (see the fallback below). System model — one
system, not two: [`../ARCHITECTURE.md`](../ARCHITECTURE.md). Original planning
history: [`CEDRIC-AVATAR-PLAN.md`](CEDRIC-AVATAR-PLAN.md) (superseded).

## Design principles (non-negotiable)

1. **Laura stays independent.** Every field below is OPTIONAL except
   `meeting_url`; with no token, no callback and no new fields, Laura behaves
   exactly as the key-free local demo.
2. **Distilled data only.** Raw transcripts are PII: they live in the local
   artifact store (served only by the local `/meetings` archive) and NEVER
   cross this API — not in webhooks, not in endpoint responses.
3. **Nothing on the hot path.** All callbacks are fire-and-forget from worker
   threads; API handling never blocks the live meeting loop.
4. **Meter safety.** Every started session ends exactly once (`end`, `cancel`,
   or meeting-over), and orchestrators can always poll the artifact.

## Authentication

- Inbound: `Authorization: Bearer <LAURA_API_TOKEN>` on every mutating or
  data-bearing call. Empty/unset token = open (local dev + demo only — ALWAYS
  set it on a hosted instance). Deliberately open even with a token set:
  `GET /avatars`, the demo console, and the local `/meetings` archive (the
  archive serves full artifacts incl. transcripts — treat a hosted instance's
  URL as sensitive until it grows its own gate).
- Outbound webhooks are signed `X-Laura-Signature: t=<unix>,v1=<hex>` where
  `v1 = HMAC_SHA256(LAURA_WEBHOOK_SECRET, "<t>.<raw_body>")`; reject when
  `|now − t| > 300s`. Plus `Authorization: Bearer <LAURA_WEBHOOK_TOKEN>` when
  set. When `LAURA_WEBHOOK_SECRET` is unset the webhook still sends but the
  signature header is ABSENT — receivers must reject unsigned events in prod.
  Laura follows one redirect hop (301/307/308), re-applying auth headers.
- `context_url` fetches present `Authorization: Bearer <LAURA_CONTEXT_TOKEN>`.
- Deployment prerequisite: the Recall dashboard webhook must point at
  `PUBLIC_BASE_URL/webhooks/recall` — bot status events drive `session.status`,
  the `context_url` refresh, and auto-finalize on meeting end.

## Inbound endpoints (orchestrator → Laura)

| Endpoint | Purpose |
|---|---|
| `POST /sessions/start` | Book/schedule an avatar into a meeting |
| `POST /sessions/{bot_id}/end` | Finalize now → returns the distilled artifact |
| `POST /sessions/{bot_id}/cancel` | Drop a booking/live bot, no artifact |
| `POST /sessions/{bot_id}/context` | **Live context push** — replace the session's brief mid-meeting the moment something changes. Body `{"context": {"meeting"?, "brief_markdown"?, "mission"?}}` (same shape as `start`); each push REPLACES the brief (re-summarize upstream, byte cap applies) and the avatar's next answer speaks from it. Also resets the periodic pull window. Per-org bearers only reach their own org's sessions (foreign = 404). `200 {ok, brief_bytes}`, `400` oversize/empty. |
| `GET /sessions/{bot_id}/artifact` | Poll: `{status:"in_progress"}` → `{status:"done", …}` (404 unknown) |
| `GET /avatars` | List installed avatars: `{id, name, role, wake_words}` |
| `GET /ledger?meeting_url=…` | Cross-meeting items + carryover brief for one link |
| `GET /org/brief?meeting_url=…` | The carryover brief (what past meetings left open) |
| `GET /org/actions` | Open action items across meetings, grouped by meeting key |
| `GET /org/search?q=…` | Ask across every meeting — ledger items + meeting snippets mentioning the query ("what did we decide about pricing?") |
| `POST /org/actions/{id}/resolve` | Close an item from the outside (e.g. ticked in Slack). `{id}` is EITHER the numeric ledger row id OR the stable string `action_id` an action carries on `action.requested` / `actions[]` — use the `action_id` to ack an action you executed. `200 {resolved:true}`, `404` unknown/already-resolved (resolves only after the meeting finalized). |
| `POST /org/chat` | Post into the org's **dashboard chat channel** (the in-dashboard approval surface). Body is exactly one of `{"message": {"text", "sender_label"?}}` or `{"action_card": {"action_id", "item", "owner"?, "due"?, "note"?}}`. An action card renders with inline Approve & run / Reject in the dashboard — the decision still lands on the canonical approve door, this endpoint only carries the conversation. `200 {ok:true, id}`, `400` bad shape. Distilled content only. |

### `POST /sessions/start`

```jsonc
{
  "meeting_url": "https://meet.google.com/abc-defg-hij",   // REQUIRED — the only required field
  // ── everything below is optional ──
  "join_at": "2026-07-09T15:00:00+02:00",  // omit = join now; ≥10 min out = schedule
  "avatar_id": "cedric",                   // which avatars/<id>/ pack (default: DEFAULT_AVATAR_ID)
  "context": {
    "meeting": { "title": "Q3 sync", "attendees": [ … ] },   // opaque to the live loop
    "brief_markdown": "## Why this meeting …"                // ≤32KB; grounds live answers AND the summary
  },
  "callback_url": "https://…/webhook",     // where session.status / session.ended are POSTed
  "context_url":  "https://…/context?s=…", // Laura GETs a fresh brief when the bot reaches the call
  "external_ref": { "any": "opaque" }      // stored, echoed verbatim in every webhook
}
```

`200 → {"bot_id", "conversation_id", "avatar_page_url", "scheduled_for"|null}`
Errors: `400` bad URL / oversized brief / upstream (Recall) failure — all
start-side failures currently surface as 400 · `401` missing/bad token ·
`409` a session already exists for this `meeting_url` — the body carries the
existing `bot_id` so you can cancel-then-rebook. (The 409 dedupe is checked
against THIS instance's session store; treat it as best-effort, not a global
lock.) `end`/`cancel` return `404 {"error":"unknown bot_id"}` for unknown ids;
`end` is idempotent and re-returns the stored artifact. The artifact poll
answers `in_progress` for ANY live session — including a scheduled bot that
has not joined yet.

The bot's name tile is the avatar's `name` from its `avatar.yaml` (wake words
too — surface them to end users via `GET /avatars` so nobody calls the wrong
name in the meeting).

## Browser operator (B0, flag-gated)

With `BROWSER_OPERATOR_ENABLED=true` (+ control plane), Laura serves a
browser-session surface under the same machine gate as `/org/*` (bearer +
org resolution; client-supplied `org_id` mismatch = 403) and a strict
cookie/same-origin dashboard twin at `/dashboard/browser/*`. Flag off ⇒ every
route 404s and the demo is byte-identical. Full contracts + fixtures:
`frontend/fixtures/browser_states.json`,
`docs/product/BROWSER-B0-EVALUATION.md`.

| Endpoint | Purpose |
|---|---|
| `POST /org/browser/sessions` | Create a session `{avatar_key?, meeting_ref?}` → `{session}` (Laura uuid only — provider ids are never exposed) |
| `GET /org/browser/sessions/{id}` | Session state (`creating/ready/presenting/closing/closed/failed/expired/revoked`) |
| `POST /org/browser/sessions/{id}/commands` | `{verb: observe\|navigate\|click\|type\|scroll, command_id?, url?, element_id?, text?, verify?, expected?}` — idempotent on `command_id`; read-only verbs execute, guarded writes are rejected (B0) or become canonical actions (`BROWSER_ALLOW_WRITES`); `verify+expected` runs post-op visual verification. Returns the stable command-result contract (accepted/command_sequence/page_version/classification/observation/action_id/failure_category/replanning_permitted/verification) |
| `GET /org/browser/sessions/{id}/observation` | Stable `BrowserObservation` (session_id/command_sequence/page_version/url/title/viewport/screenshot_ref/dom_summary/visible_text/elements/truncated/timestamp) |
| `POST /org/browser/sessions/{id}/metadata` | Set bounded, NON-authoritative demo-run metadata (demo_definition_id/version, demo_run_id, current_checkpoint) — never drives state |
| `POST /org/browser/sessions/{id}/present` | Mint a one-shot opaque presentation token (short TTL, revocable, sha256-stored) |
| `POST /org/browser/present/exchange` | `{presentation_token}` → current READ-ONLY viewer payload (server-side; denies replay/expiry/revoked/cross-org) |
| `POST /org/browser/sessions/{id}/close` | Idempotent close (provider released, tokens revoked) |
| `POST /org/browser/sessions/{id}/revoke` | Idempotent security stop |

Guarded browser steps approve through the EXISTING action doors
(`/org/actions/{id}/approve`, `/dashboard/actions/{id}/approve`) — there is
no browser-specific approval surface.

## Outbound webhooks (Laura → orchestrator)

POSTed to the session's `callback_url`, signed as above, `external_ref` echoed.

| Event | Delivery | Payload core |
|---|---|---|
| `session.status` | best-effort, single attempt | `{event, bot_id, external_ref, status: "joining"\|"live"\|"failed", detail, at}` (`detail` always present, may be `""`; `failed` fires only for a fatal join) |
| `action.requested` | best-effort, single attempt | `{event, bot_id, external_ref, action_id, action, owner, due, at}` — fired the moment someone asks the avatar to DO something mid-meeting, so the approval card is ready before the call ends. **`action_id`** is a stable id: the SAME action appears in the later `session.ended` `actions[]` carrying the same `action_id`, so **dedupe your live card against the final action on `action_id`, not on text** (the wording can still be extended after this event fired). The artifact's `actions[]` stays the authoritative list (live captures are flagged `requested_live: true`). |
| `session.ended` | retried 3× (5s / 25s / 2m), then poll fallback | `{event, bot_id, external_ref, ended_at, artifact}` |
| `chat.message` | best-effort, single attempt (conversational — the human resends) | `{event, org_id, message_id, text, sender, event_id, at}` — a dashboard user wrote to the orchestrator in the **dashboard chat channel**. POSTed to the per-org events door (`…/api/laura/events`), signed like every event. Reply (and propose actions) via `POST /org/chat`; approvals for cards you post there converge on the canonical approve door like every other surface. |
| `action.approved` | best-effort, single attempt (the action stays `approved` + retryable on the dashboard if missed) | `{event, org_id, action_id, bot_id, decided_via, action, owner, due, event_id, at}` — a human decision landed on Laura's side and the orchestrator should EXECUTE now. `decided_via: "voice"` = **voice consent**: someone addressed the avatar by name mid-meeting ("Petra, create a task for X") and that spoken, addressed ask was recorded as the canonical approval (first write wins; a pre-existing decision suppresses this event). Fires the moment the ask is captured — execute, then report back via `POST /org/actions/{action_id}/status` so the receipt reaches the dashboard. |

### The artifact (wire shape, additive)

`{artifact_version: 1, summary, actions[], checklist, decisions[], risks[],
missing_steps[], readiness_score, follow_up_email, meeting_type?,
participation[]?}`

- **No `transcript` field, ever** (see principle 2). The same distilled copy
  is returned by `POST …/end` and `GET …/artifact`.
- `actions[]` items are `{action_id, owner, item, deadline?, gap_type?,
  requested_live?}` (a legacy item may be a plain string with no id) — parse
  defensively; `deadline` is not guaranteed on every item. **`action_id`** is
  the stable key: it matches the `action.requested` you saw live (dedupe on it)
  and is what you pass back to `POST /org/actions/{action_id}/resolve` to ack an
  action you executed.
- `meeting_type` and `participation` appear only when the meeting produced a
  transcript; a silent/empty meeting yields the seed shape and
  `follow_up_email` may be `{}`. Parse all fields as optional.
- New fields will be added; existing ones never change meaning
  (`artifact_version` bumps only on breaking change, which we avoid).

## Context refresh (live pull + push)

With `context_url` set, Laura GETs it when the bot reaches the call **and
keeps re-pulling for the whole meeting**: while transcripts are arriving, the
brief is re-fetched whenever the last pull is older than
`CONTEXT_REFRESH_SECONDS` (default 120; `0` restores the old one-shot
join-time pull). A quiet meeting stops pulling. Serve current content on
`context_url` and the avatar's grounding stays live with zero orchestrator
changes. For real-time updates, push instead: `POST
/sessions/{bot_id}/context` (above) — a push also resets the pull window, so
pushing orchestrators aren't double-polled. Any pull error → the last good
brief stays.

## Standalone (no orchestrator connected)

`{meeting_url}` alone (+ token): the default avatar joins, no brief. With no
orchestrator connected Laura still senses and builds the artifact (poll it via
`GET …/artifact`), but there is **no autonomous execution** — she never acts. The
optional `AUTOPILOT_*` notetaker can email/Slack a recap when a meeting ends, but
never executes the agreed actions — see
[`../ARCHITECTURE.md`](../ARCHITECTURE.md). Orchestrated sessions skip that
autopilot — Cedric owns approval-gated delivery and all execution.

## Env vars (server side)

`LAURA_API_TOKEN`, `LAURA_WEBHOOK_SECRET`, `LAURA_WEBHOOK_TOKEN`,
`LAURA_CONTEXT_TOKEN`, `DEFAULT_AVATAR_ID`, `CALLBACK_TIMEOUT_SECONDS` — see
`.env.example`. In production these live in SSM; never in git.

## Known limits / roadmap

- Single shared token (Cedric is the one connected orchestrator today). A
  per-client key registry keeps this API shape and lands if a second
  orchestrator ever shows up.
- Ledger/artifacts AND live session state live in sqlite on an ephemeral
  disk — org memory dies on deploy, and a deploy mid-meeting loses the
  session (no `session.ended` fires; poll + your watchdog are the backstop).
  Durability decision (Litestream→S3 / Postgres) pending.
- `POST /sessions/{bot_id}/deliver` (auth-gated) exists for Laura's own
  autopilot delivery (email + Slack); orchestrators normally ignore it.
