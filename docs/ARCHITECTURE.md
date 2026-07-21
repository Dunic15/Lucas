# Architecture, the Laura + Cedric system

> **Single source of truth for the *system model***, how Laura and Cedric fit
> together as one product. For Laura's internal engine (request paths, model
> routing, RAG, avatar face, deploy topology) see
> [`ARCHITECTURE_CURRENT.md`](ARCHITECTURE_CURRENT.md). For the HTTP wire contract
> see [`integration/SURFACE-API.md`](integration/SURFACE-API.md).
> Last aligned: 2026-07-09.

## One system, two halves (not two models)

Laura and Cedric are **one product with a clean split of responsibility**: the
senses and the brain of a single AI employee:

- **Laura = the meeting avatar (senses).** Joins Zoom/Meet/Teams, listens,
  answers grounded questions, and silently gathers decisions + the actions people
  agree to. **Laura never acts**: she observes and distils.
- **Cedric = the Slack agent (brain + hands).** Receives Laura's distilled
  artifact + the agreed actions and **executes them** with his own tools
  (Slack posts, calendar holds, email, Pipedream, …).

```
                pre-meeting context ①            post-meeting handoff ③
   ┌─────────┐  ◄─────────────────────  ┌─────────┐  ─────────────────────►  ┌─────────┐
   │ Cedric  │   who's-who + brief       │  Laura  │   session.ended artifact  │ Cedric  │
   │ (brain) │                           │(senses) │   + action.requested      │ (hands) │
   └─────────┘                           └─────────┘                           └─────────┘
                                          ② listens silently,                   auto-executes
                                          captures decisions +                  the agreed actions
                                          agreed actions
```

This is **one system**, not a menu of two architectures. Laura runs as **Model A**:
she senses and hands off, and Cedric executes with his own tools. An earlier
autonomous shape; historically "Model B," the avatar's own `EXECUTE_*` execution
pack (self-sending email, filing Drive docs, booking calendar holds, auto-running
actions); has been **removed**. The paired hand-off is the only deployment; Laura
never acts.

## The three phases

**① Pre-meeting: Laura pulls context from Cedric.**
When a session names a `context_url` (or the default `SURFACE_CONTEXT_URL` is
set), Laura GETs `{context: {meeting, brief_markdown}}` from Cedric the moment the
bot reaches the call: "who's who + why this meeting" straight from Cedric's
memory. No context source → Laura falls back to her own local brief (Drive/ledger
carryover). One HTTP GET, off the live path.

**② In-meeting: Laura senses, silently.**
Laura tracks every line into `MeetingState` (pure regex, zero added latency),
answers groundable questions, and, via the platform `queue_action` tool,
captures each action someone asks for (`{action, owner, due}`) in memory. She
**acknowledges but never executes** ("I'll queue that for approval in Slack right
after the call"). The people agreeing to the action *in the meeting* **is the
approval.**

**③ Post-meeting: Laura hands off, Cedric executes.**
On meeting end Laura POSTs to the session's `callback_url` (or default
`SURFACE_WEBHOOK_URL`):
- `action.requested`: fired the moment an action is captured mid-meeting, so
  Cedric's approval card is ready before the call even ends.
- `session.ended`: the distilled artifact (summary, `actions[]`, decisions,
  risks, follow-up email, …). **Never the raw transcript**: transcripts are PII
  and never cross the API.

Cedric receives these at `…/api/laura/events`, verifies the HMAC signature, and
**auto-executes the agreed actions** with his own tools. Because the in-meeting
agreement already served as approval, Cedric can act without a second round-trip.

Delivery is retried and fire-and-forget off the live path; orchestrated sessions
**skip Laura's own autopilot** entirely. Cedric owns delivery.

## Config that turns paired mode on

Set two env vars on the Laura backend (App Runner service env / SSM):

| Env var | Effect |
|---|---|
| `SURFACE_WEBHOOK_URL` | Default `callback_url` for every session → each meeting's `session.ended` + `action.requested` go to Cedric's `…/api/laura/events`. **Setting this is what makes the system "paired."** |
| `SURFACE_CONTEXT_URL` | Default `context_url` → Laura pulls "who's who + context" from Cedric's `…/api/laura/context` at join. |

A per-session `callback_url` / `context_url` in the `POST /sessions/start` body
always wins over these defaults. Auth for the seam: `LAURA_API_TOKEN` (inbound
Bearer), `LAURA_WEBHOOK_SECRET` + `LAURA_WEBHOOK_TOKEN` (outbound signing),
`LAURA_CONTEXT_TOKEN` (context pull). Full field-by-field contract:
[`integration/SURFACE-API.md`](integration/SURFACE-API.md).

## Standalone (no Cedric connected)

When **no** Cedric receiver is configured (`SURFACE_WEBHOOK_URL` unset), Laura
simply doesn't hand off: she still joins, senses, and builds the distilled
artifact (pollable via `GET …/artifact`), but there is **no autonomous execution**.
The avatar's own `EXECUTE_*` "execution pack", self-sending email, filing Drive
notes, booking calendar holds, auto-running actions, was **removed**; Laura never
acts. The optional `AUTOPILOT_*` notetaker can still email/Slack a recap when a
meeting ends, but it too never executes the agreed actions; execution is always
Cedric's job.

## Pointers

- **Wire contract (endpoints, webhook shapes, auth):**
  [`integration/SURFACE-API.md`](integration/SURFACE-API.md); the live v1 API.
- **Laura's internal engine:** [`ARCHITECTURE_CURRENT.md`](ARCHITECTURE_CURRENT.md).
- **Historical planning (how the platform + Cedric came together):**
  [`integration/CEDRIC-AVATAR-PLAN.md`](integration/CEDRIC-AVATAR-PLAN.md)
  (superseded by this doc).
