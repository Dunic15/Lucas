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
| `GET /sessions/{bot_id}/artifact` | Poll: `{status:"in_progress"}` → `{status:"done", …}` (404 unknown) |
| `GET /avatars` | List installed avatars: `{id, name, role, wake_words}` |
| `GET /ledger?meeting_url=…` | Cross-meeting items + carryover brief for one link |
| `GET /org/brief?meeting_url=…` | The carryover brief (what past meetings left open) |
| `GET /org/actions` | Open action items across meetings, grouped by meeting key |
| `GET /org/search?q=…` | Ask across every meeting — ledger items + meeting snippets mentioning the query ("what did we decide about pricing?") |
| `POST /org/actions/{id}/resolve` | Close an item from the outside (e.g. ticked in Slack). `{id}` is EITHER the numeric ledger row id OR the stable string `action_id` an action carries on `action.requested` / `actions[]` — use the `action_id` to ack an action you executed. `200 {resolved:true}`, `404` unknown/already-resolved (resolves only after the meeting finalized). |

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

## Outbound webhooks (Laura → orchestrator)

POSTed to the session's `callback_url`, signed as above, `external_ref` echoed.

| Event | Delivery | Payload core |
|---|---|---|
| `session.status` | best-effort, single attempt | `{event, bot_id, external_ref, status: "joining"\|"live"\|"failed", detail, at}` (`detail` always present, may be `""`; `failed` fires only for a fatal join) |
| `action.requested` | best-effort, single attempt | `{event, bot_id, external_ref, action_id, action, owner, due, at}` — fired the moment someone asks the avatar to DO something mid-meeting, so the approval card is ready before the call ends. **`action_id`** is a stable id: the SAME action appears in the later `session.ended` `actions[]` carrying the same `action_id`, so **dedupe your live card against the final action on `action_id`, not on text** (the wording can still be extended after this event fired). The artifact's `actions[]` stays the authoritative list (live captures are flagged `requested_live: true`). |
| `session.ended` | retried 3× (5s / 25s / 2m), then poll fallback | `{event, bot_id, external_ref, ended_at, artifact}` |

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

## Context refresh (optional pull)

With `context_url` set, Laura GETs it once when the bot reaches the call and
swaps in the returned `{context: {meeting, brief_markdown}}` — a fresh brief
for bookings made days earlier. Any error → the booking-time brief stays.

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
