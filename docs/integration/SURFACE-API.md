# Laura Surface API — v1 (implemented)

**Status: LIVE contract.** This documents what the code on `main` actually
serves. Laura is a standalone meeting-avatar service; any number of
orchestrators drive it over this HTTP API — Cedric (meet-cedric.com) is
client #1. History and rationale: [`CEDRIC-AVATAR-PLAN.md`](CEDRIC-AVATAR-PLAN.md).

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
  set it on a hosted instance).
- Outbound webhooks are signed `X-Laura-Signature: t=<unix>,v1=<hex>` where
  `v1 = HMAC_SHA256(LAURA_WEBHOOK_SECRET, "<t>.<raw_body>")`; reject when
  `|now − t| > 300s`. Plus `Authorization: Bearer <LAURA_WEBHOOK_TOKEN>` when
  set. Laura follows one permanent-redirect hop, re-applying auth headers.
- `context_url` fetches present `Authorization: Bearer <LAURA_CONTEXT_TOKEN>`.

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
| `POST /org/actions/{id}/resolve` | Close an item from the outside (e.g. ticked in Slack) |

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
Errors: `400` bad URL / oversized brief · `401` missing/bad token ·
`409` a session already exists for this `meeting_url` (cancel first — one bot
per meeting URL, globally).

The bot's name tile is the avatar's `name` from its `avatar.yaml` (wake words
too — surface them to end users via `GET /avatars` so nobody calls the wrong
name in the meeting).

## Outbound webhooks (Laura → orchestrator)

POSTed to the session's `callback_url`, signed as above, `external_ref` echoed.

| Event | Delivery | Payload core |
|---|---|---|
| `session.status` | best-effort, single attempt | `{event, bot_id, external_ref, status: "joining"\|"live"\|"failed", detail?, at}` |
| `session.ended` | retried 3× (5s / 25s / 2m), then poll fallback | `{event, bot_id, external_ref, ended_at, artifact}` |

### The artifact (wire shape, additive)

`{artifact_version: 1, summary, actions[] (owner, item, deadline), checklist,
decisions[], risks[], missing_steps[], readiness_score, meeting_type,
follow_up_email{subject, body}, participation[]}`

- **No `transcript` field, ever** (see principle 2). The same distilled copy
  is returned by `POST …/end` and `GET …/artifact`.
- `actions[]` items may be plain strings or `{owner, item, deadline}`.
- New fields will be added; existing ones never change meaning
  (`artifact_version` bumps only on breaking change, which we avoid).

## Context refresh (optional pull)

With `context_url` set, Laura GETs it once when the bot reaches the call and
swaps in the returned `{context: {meeting, brief_markdown}}` — a fresh brief
for bookings made days earlier. Any error → the booking-time brief stays.

## Autonomous mode

`{meeting_url}` alone (+ token): the default avatar joins, no brief, and
Laura's own autopilot (if enabled) handles delivery. Orchestrated sessions
skip autopilot — the orchestrator owns approval-gated delivery.

## Env vars (server side)

`LAURA_API_TOKEN`, `LAURA_WEBHOOK_SECRET`, `LAURA_WEBHOOK_TOKEN`,
`LAURA_CONTEXT_TOKEN`, `DEFAULT_AVATAR_ID`, `CALLBACK_TIMEOUT_SECONDS` — see
`.env.example`. In production these live in SSM; never in git.

## Known limits / roadmap

- Single shared token (client #1 = Cedric). A per-client key registry keeps
  this API shape and lands when a second orchestrator shows up.
- `action.requested` live event (approval card ready before the meeting ends)
  — next, see plan §P6.
- Ledger/artifacts live in sqlite on an ephemeral disk — org memory dies on
  deploy. Durability decision (Litestream→S3 / Postgres) pending.
