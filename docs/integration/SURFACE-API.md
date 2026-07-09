# Laura Surface API — v1 (implemented)

**Status: LIVE contract.** This documents what the code on `main` actually
serves — precise enough to integrate against without reading Laura's source.
Laura is the meeting-avatar (senses) half of the **Laura + Cedric system**: she
joins the call, senses it, and hands the distilled artifact + agreed actions to
an orchestrator (Cedric, the Slack brain/hands — client #1) over this HTTP API.
The API stays open to a second orchestrator, and Laura can also run standalone
(§8). System model: [`../ARCHITECTURE.md`](../ARCHITECTURE.md). Planning
history: [`CEDRIC-AVATAR-PLAN.md`](CEDRIC-AVATAR-PLAN.md) (superseded).

> **Direction of calls.** _Orchestrator → Laura_ is plain REST with a Bearer
> token (§2, §3). _Laura → Orchestrator_ is signed webhooks (§4). The HMAC
> signing scheme applies **only** to the webhooks Laura sends; there is no
> inbound webhook from the orchestrator to Laura.

---

## 1. Basics

| | |
|---|---|
| **Base URL (prod)** | `https://dhfgfe6yw6.eu-central-1.awsapprunner.com` (AWS App Runner, eu-central-1). This is the deployment's `PUBLIC_BASE_URL`; a self-hosted instance sets its own. Local dev default: `http://127.0.0.1:8000`. |
| **Transport** | HTTPS. All paths below are relative to the base URL. |
| **Content type** | `application/json` for every request body and every response (responses are always JSON). Send `Content-Type: application/json` on POSTs with a body. |
| **Auth (inbound)** | `Authorization: Bearer <LAURA_API_TOKEN>` on every endpoint in §3. See §2. |
| **Field naming** | `snake_case` everywhere, in both directions (`bot_id`, `external_ref`, `meeting_url`, `brief_markdown`, `follow_up_email`, `readiness_score`, …). |
| **Timestamps** | ISO 8601 **with offset**. Outbound timestamps (`at`, `ended_at`) are UTC (`+00:00`); the microsecond fraction is present **only when nonzero** — Python `isoformat()` drops it on an exact-second boundary (e.g. `2026-07-09T13:00:00+00:00`), so parse the `.ffffff` component as **optional**. Inbound `join_at` is an opaque string forwarded to Recall unmodified — send ISO 8601 with offset, e.g. `2026-07-09T15:00:00+02:00`. |
| **IDs** | `bot_id` is the Recall bot id (an opaque string); it is the stable handle for a session across its whole lifecycle and every webhook. `item_id` (ledger) is an integer. |

---

## 2. Authentication

### Inbound (you → Laura)

- Header: **`Authorization: Bearer <LAURA_API_TOKEN>`**, compared in constant
  time against the exact string `Bearer <LAURA_API_TOKEN>`.
- If `LAURA_API_TOKEN` is **unset**, the API is **open** (local dev + zero-key
  demo only — always set it on a hosted instance).
- On mismatch: **`401 {"error": "unauthorized"}`**.
- Deliberately open even when a token is set: `GET /avatars`, the demo console,
  and the local `/meetings` archive (that archive serves full artifacts
  **including transcripts** — treat a hosted instance's URL as sensitive until
  it grows its own gate).

### Outbound (Laura → you): Bearer + HMAC

Every webhook Laura POSTs to your `callback_url` carries:

- **`Authorization: Bearer <LAURA_WEBHOOK_TOKEN>`** — a cheap first-line check
  (only sent when `LAURA_WEBHOOK_TOKEN` is configured).
- **`X-Laura-Signature: t=<unix_ts>,v1=<hmac_hex>`** — Slack/Stripe-style.
  - `t` = integer Unix seconds at send time.
  - `v1` = `HMAC_SHA256(key = LAURA_WEBHOOK_SECRET, msg = "<t>." + <raw_request_body_bytes>)`, lowercase hex.
  - **Signed string** = the ASCII timestamp, a literal `.`, then the **exact
    raw JSON body bytes** — verify against the bytes you received, before any
    re-serialization.
  - **Replay window (receiver-enforced): reject if `|now − t| > 300s`.**
  - When `LAURA_WEBHOOK_SECRET` is **unset**, the webhook is still sent but the
    `X-Laura-Signature` header is **absent** — a production receiver must reject
    unsigned events.

Verification (reference):

```python
import hmac, hashlib
def valid(raw_body: bytes, header: str, secret: str, now: int) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))  # {"t":..,"v1":..}
    t = int(parts["t"])
    if abs(now - t) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])
```

### Context pull (Laura → your `context_url`)

- Header: **`Authorization: Bearer <LAURA_CONTEXT_TOKEN>`** (only when set). No
  HMAC on this GET. See §5.

### Redirects

For both webhooks and the context pull, Laura follows **one** redirect hop
(301 / 307 / 308) and **re-applies** the auth + signature headers to the new URL
(httpx strips `Authorization` on cross-host redirects, so this is deliberate).
Point `callback_url` / `context_url` at the final host to avoid the extra hop.

### Deployment prerequisite

The Recall dashboard webhook must point at
**`PUBLIC_BASE_URL/webhooks/recall`**. Recall bot-status events are what drive
`session.status`, the one-time `context_url` refresh, and auto-finalize on
meeting end. Without it, `session.ended` still fires on an explicit
`POST …/end`, but live status relays and auto-finalize won't.

---

## 3. Endpoints (Orchestrator → Laura)

Every endpoint here is behind the §2 inbound Bearer gate. Two error-shape
families you will see:

- **Application errors**: `{"error": "<message>"}` with a 4xx status (what
  Laura's handlers return: 400 / 401 / 404 / 409).
- **Request-validation errors**: FastAPI's default **`422`** with
  `{"detail": [ … ]}` when the JSON body/path fails schema validation (e.g.
  `meeting_url` missing, `external_ref` not an object, `item_id` not an
  integer). This shape is distinct from `{"error": …}` — handle both.

| Method & path | Purpose |
|---|---|
| `POST /sessions/start` | Book / schedule an avatar into a meeting |
| `POST /sessions/{bot_id}/end` | Finalize now → returns the distilled artifact |
| `POST /sessions/{bot_id}/cancel` | Drop a booking / live bot, **no** artifact |
| `GET /sessions/{bot_id}/artifact` | Poll for the artifact |
| `GET /org/actions` | Open action items across all meetings, grouped |
| `GET /org/brief?meeting_url=…` | Carryover brief for one meeting link |
| `POST /org/actions/{item_id}/resolve` | Close a ledger item from outside |
| _also available:_ `GET /avatars` · `GET /ledger?meeting_url=…` · `GET /org/search?q=…` · `POST /sessions/{bot_id}/deliver` | See §7 |

### 3.1 `POST /sessions/start`

Book (or schedule) an avatar into a meeting. **`meeting_url` is the only
required field**; everything else is optional and, omitted, degrades to the
key-free local behaviour.

**Request body**

```jsonc
{
  "meeting_url": "https://meet.google.com/abc-defg-hij",  // REQUIRED

  // ── all optional ──
  "avatar_id":   "cedric",                     // which avatars/<id>/ pack; "" or omitted → DEFAULT_AVATAR_ID
  "join_at":     "2026-07-09T15:00:00+02:00",  // ISO 8601 w/ offset. Omit = join now. Future = schedule (see lead time)
  "callback_url":"https://cedric.example/api/laura/events",   // where §4 webhooks are POSTed
  "context_url": "https://cedric.example/api/laura/context?s=abc", // GET once at join for a fresh brief (§5)
  "external_ref":{ "any": "opaque", "json": "object" },       // stored & echoed verbatim in every webhook (§6)
  "context": {
    "meeting":  { "title": "Q3 sync", "starts_at": "…", "attendees": ["…"] }, // opaque metadata; folded into the brief
    "brief_markdown": "## Why this meeting …"  // ≤ 32768 bytes; grounds live answers AND the post-meeting summary
  }
}
```

**Accepted fields (exact)**

| Field | Type | Notes |
|---|---|---|
| `meeting_url` | string | **Required.** Meet/Zoom/Teams URL. |
| `avatar_id` | string | Default `""` → server's `DEFAULT_AVATAR_ID` (`laura`). Unknown id → `400`. |
| `join_at` | string (ISO 8601 + offset) or null | Omit/null = join immediately. |
| `callback_url` | string or null | No `callback_url` **and** no `SURFACE_WEBHOOK_URL` default configured ⇒ no webhooks (standalone). |
| `context_url` | string or null | See §5. |
| `external_ref` | object or null | Opaque; **echoed verbatim** in every callback (§6). Defaults to `{}`. |
| `context.meeting` | object | Opaque to the live loop; stored on the session, surfaced to the summary. |
| `context.brief_markdown` | string | The pre-meeting brief. **Max 32768 bytes (32 KB), UTF-8.** Over → `400`. |

**Scheduling lead time.** For `join_at` to schedule rather than join now, it
must be **≥ 10 minutes in the future** — this is Recall's requirement for a
scheduled bot. Laura passes `join_at` through to Recall unmodified and does not
itself reject a shorter lead time; a too-soon `join_at` is rejected upstream and
surfaces as a `400` (see errors). Use a comfortable margin.

**Success — `200`**

```json
{
  "bot_id": "b7f3…",                         // the session handle for everything downstream
  "conversation_id": "9c1a…",                // internal avatar-page ws key; informational
  "avatar_page_url": "https://…/talk?…",     // the page Recall renders as the bot camera; informational
  "scheduled_for": "2026-07-09T15:00:00+02:00" // echoes join_at, or null when joining now
}
```

**Status codes**

| Code | When | Body |
|---|---|---|
| `200` | Booked / scheduled | success object above |
| `400` | Recall not configured/ready; unknown `avatar_id`; bad `meeting_url`; oversized brief; any upstream (Recall) create-bot failure | `{"error": "<message>"}` — oversized brief is exactly `{"error": "context.brief_markdown exceeds 32768 bytes"}` |
| `401` | Missing/bad `LAURA_API_TOKEN` | `{"error": "unauthorized"}` |
| `409` | A session already exists for this `meeting_url` | `{"error": "a session already exists for this meeting_url", "bot_id": "<existing>"}` |
| `422` | Body fails schema (e.g. `meeting_url` missing) | `{"detail": [ … ]}` |

> **All start-side failures currently surface as `400`** (including upstream
> Recall errors) except the four specific cases above — do not rely on the
> status code to distinguish "bad request" from "Recall hiccup"; read `error`.

**`409` "session already exists" behaviour.** One live-or-scheduled booking per
`meeting_url`. If you POST `start` for a `meeting_url` that already has a session
in **this instance's** store, you get `409` with the existing `bot_id` in the
body. **To rebook: `POST /sessions/{that bot_id}/cancel` first, then `start`
again.** The check is against a single instance's in-memory/SQLite store — treat
it as best-effort dedupe, **not** a global distributed lock.

### 3.2 `POST /sessions/{bot_id}/end`

Finalize the session **now**: stops both vendor meters (Recall + legacy Anam),
builds and stores the artifact, folds facts into the cross-meeting ledger, and
(for orchestrated sessions) fires `session.ended`. **Idempotent** — calling it
again re-returns the stored artifact.

- No request body.
- **`200`** → the distilled artifact (§4.4 wire shape, **no transcript**).
- **`202 {"ok": true, "finalizing": "<bot_id>"}`** → the session is mid-finalize
  on another path (a terminal Recall webhook or the reconcile loop got there
  first). Not an error — poll `GET …/artifact`.
- **`404 {"error": "unknown bot_id"}`** → no such session and no stored artifact.
- **`401`** on auth failure.

### 3.3 `POST /sessions/{bot_id}/cancel`

Drop a **scheduled** bot before it joins, or abort a **live** one, **without**
building an artifact. Use this when a calendar event moves or is cancelled (then
rebook). Contrast with `end`, which finalizes + returns the artifact.

- No request body. Stops the meter (best-effort: `leave_call`, falling back to
  deleting a not-yet-joined scheduled bot). The session is removed either way
  and **never** produces an artifact or a `session.ended` webhook.
- **`200 {"cancelled": true, "bot_id": "<bot_id>"}`**.
- **`404 {"error": "unknown bot_id"}`** → no such session.
- **`401`** on auth failure.

### 3.4 `GET /sessions/{bot_id}/artifact`

Poll for a session's outcome. Your fallback whenever a `session.ended` webhook
was missed.

- **In progress** (session still live/scheduled): **`200 {"status": "in_progress", "bot_id": "<bot_id>"}`** — note this also covers a scheduled bot that hasn't joined yet.
- **Done**: **`200 {"status": "done", …artifact}`** — the same distilled wire
  artifact as `end` (§4.4), spread alongside `status`. **No transcript.**
- **Unknown**: **`404 {"error": "unknown bot_id"}`**.
- **`401`** on auth failure.

### 3.5 `GET /org/actions`

Every **open** ledger item across all meetings, grouped by `meeting_key`. This
is org-memory, not per-session — distilled lines only, never transcript. Because
it filters to open items, **every row here has `status: "open"` and `kind` is
only `"action"` or `"missing_step"`** — decisions never appear (they're stored
`noted`; query `GET /ledger` or `GET /org/search` for those).

- **`200`**:

```jsonc
{
  "open": {
    "meet.google.com/abc-defg-hij": [        // key = ledger.meeting_key(meeting_url)
      {
        "id": 42,                            // ledger item id → use in /org/actions/{id}/resolve
        "meeting_key": "meet.google.com/abc-defg-hij",
        "avatar_id": "cedric",
        "kind": "action",                    // here always "action" | "missing_step" (decisions are never open)
        "item": "Send DPA to vendor",        // distilled line — never raw transcript
        "owner": "Dana",                     // "" OR the literal "UNASSIGNED" — stored verbatim; treat both as unassigned
        "deadline": "2026-07-15",            // "" if none; free-text as captured
        "meeting_type": "vendor_onboarding", // "" if not classified
        "status": "open",                    // always "open" here (column domain is open|done|noted)
        "bot_id": "b7f3…",                   // session that recorded it
        "created_at": 1752000000.0,          // epoch seconds (float)
        "resolved_at": null,                 // epoch seconds or null
        "resolved_by_bot_id": ""
      }
    ]
  }
}
```

- Empty when nothing is open: `{"open": {}}`.
- **`401`** on auth failure.

### 3.6 `GET /org/brief?meeting_url=…`

The carryover brief for one meeting link: what previous sessions of that link
left open (unconfirmed process steps, open actions with owners, recent
decisions), as a ready-to-inject markdown/plain-text block.

- Query param: **`meeting_url`** (required).
- **`200 {"meeting_key": "<key>", "brief": "<string>"}`** — `brief` is `""`
  when the link has no history.
- **`401`** on auth failure.

### 3.7 `POST /org/actions/{item_id}/resolve`

Close a single ledger item from the outside (e.g. someone ticked it off in
Slack). `item_id` is the integer `id` from `GET /org/actions`.

- No request body.
- **`200 {"resolved": true, "id": <item_id>}`**.
- **`404 {"error": "unknown or already resolved item"}`** → unknown id, or the
  item is no longer `open` (already `done`, or a `noted` decision — resolve only
  acts on `open` rows).
- **`422`** if `item_id` isn't an integer.
- **`401`** on auth failure.

---

## 4. Webhooks (Laura → Orchestrator)

When a session has a `callback_url` (supplied on `start`, or the deployment-wide
`SURFACE_WEBHOOK_URL` default), Laura POSTs lifecycle events there. Signed &
authed per §2. `external_ref` is echoed verbatim on every one (§6). All are
fire-and-forget from worker threads — **a webhook never blocks or fails the
meeting**, and the artifact is always available to poll as a fallback.

### 4.1 `session.status`

Join-progress relay. **Best-effort, single attempt** (no retry).

```json
{
  "event": "session.status",
  "bot_id": "b7f3…",
  "external_ref": { "any": "opaque" },
  "status": "joining",   // "joining" | "live" | "failed"
  "detail": "in_call",   // always present, may be ""; the raw Recall status_code or fatal code
  "at": "2026-07-09T13:00:00.123456+00:00"
}
```

`status` mapping from Recall bot state:

| `status` | Fires on Recall `status_code` |
|---|---|
| `joining` | `joining_call`, `in_waiting_room` |
| `live` | `in_call`, `in_call_recording`, `in_call_not_recording` |
| `failed` | `fatal` (a join that fatally failed before going live) |

### 4.2 `action.requested`

Fired the moment someone asks the avatar to **do** something mid-meeting (the
live `queue_action` tool captures it), so your approval card is ready before the
call ends. **Best-effort, single attempt.** Only the distilled action text /
owner / due ever leaves — never transcript content.

```json
{
  "event": "action.requested",
  "bot_id": "b7f3…",
  "external_ref": { "any": "opaque" },
  "action": "Book the security review with the vendor",
  "owner": "Dana",   // "" if unspecified
  "due": "Friday",   // "" if unspecified; free-text as spoken
  "at": "2026-07-09T13:20:00.518274+00:00"
}
```

> The artifact's `actions[]` in `session.ended` remains the **authoritative,
> complete** list; live captures reappear there flagged `requested_live: true`.
> Treat `action.requested` as an early heads-up, not the source of truth.

### 4.3 `session.ended`

The full distilled artifact. **Retried** — losing it means you fall back to
polling.

```json
{
  "event": "session.ended",
  "bot_id": "b7f3…",
  "external_ref": { "any": "opaque" },
  "ended_at": "2026-07-09T13:45:00.204871+00:00",
  "artifact": { … see 4.4 … }
}
```

### 4.4 The artifact (wire shape)

Distilled, additive, **never contains `transcript`** (stripped twice — by the
serializer and again in the webhook builder). The same object is returned by
`POST …/end` and (spread under `status:"done"`) by `GET …/artifact`.

```jsonc
{
  "artifact_version": 1,          // bumps only on a breaking change (we avoid those)
  "summary": "…",                 // string; "" for a silent/empty meeting
  "decisions": ["…"],             // array of one-line strings
  "risks": ["…"],                 // array of one-line strings
  "actions": [                    // action items — SEE PARSING NOTE
    { "item": "Send DPA", "owner": "Dana", "deadline": "2026-07-15",
      "gap_type": "owner", "requested_live": true }
  ],
  "checklist": [ … ],             // legacy alias — the SAME array as `actions`
  "missing_steps": ["…"],         // process steps the template expected but the meeting skipped
  "readiness_score": 0,           // integer 0–100, from the deterministic MeetingState tracker
  "meeting_type": "vendor_onboarding",  // classifier label; "" if unclassified
  "participation": [              // per-person, from the silent tracker (no extra model call)
    { "name": "Dana", "lines": 42, "talk_share": 55, "commitments": ["…"] }
  ],
  "follow_up_email": {            // {} for a silent meeting
    "subject": "…",
    "body": "…"
  }
}
```

**Parsing rules (important):**

- `actions[]` items may be **either** plain strings **or** objects
  `{item, owner, deadline?, gap_type?, requested_live?}` — parse defensively.
  `deadline`, `gap_type`, and `requested_live` are **not** guaranteed on every
  item. `owner` may be `"UNASSIGNED"`. `gap_type` ∈
  `{none, owner, approval, deadline, document, blocker}`.
- `checklist` is a backward-compat alias pointing at the **same** array as
  `actions` — read one, ignore the other.
- A silent / empty meeting yields the **seed shape**: `summary:""`,
  empty arrays, `readiness_score:0`, `follow_up_email:{}`, with `meeting_type`
  **and** `participation` **absent entirely** (the summary path that adds them
  never runs on a transcript-less meeting). **Parse every field as optional** and
  tolerate additive new fields.

### 4.5 Retry policy

| Event | Attempts | Backoff between attempts |
|---|---|---|
| `session.status` | 1 (no retry) | — |
| `action.requested` | 1 (no retry) | — |
| `session.ended` | up to **4** (1 initial + 3 retries) | **5 s → 25 s → 120 s** |

"Delivered" = any `2xx`. After the last `session.ended` attempt fails, Laura
gives up and relies on you polling `GET /sessions/{bot_id}/artifact`. Retries
run in a worker thread and **do not** delay finalize or the meter stop.

### 4.6 Idempotency / dedupe

There is **no dedicated idempotency-key header or field.** The stable dedupe key
is **`bot_id`**, scoped by `event`:

- **`session.ended`** — the retried event. A session ends exactly once, so
  there is **one logical `session.ended` per `bot_id`**, potentially delivered
  up to 4×. **Dedupe on `bot_id`** (treat the first `2xx`-worthy receipt as
  canonical; ignore repeats). Retries carry an identical body.
- **`session.status`** — multiple per session (e.g. `joining` then `live`).
  Dedupe on **`(bot_id, status)`** if you need at-most-once handling.
- **`action.requested`** — single-attempt, so duplicates are unlikely; if you
  dedupe, key on `(bot_id, action, owner)`.

Because `bot_id` is your join key everywhere, storing "have I processed
`session.ended` for `bot_id`?" is sufficient and idempotent.

---

## 5. The context pull (`context_url`)

An optional **Laura → your server GET** that refreshes the brief at join time —
so a booking made days ago walks in with current context.

| | |
|---|---|
| **When** | Exactly **once per session**, the first time the bot reaches a **`live`** Recall status (`in_call*`). Driven by the Recall `/webhooks/recall` events, so the dashboard webhook (§2) must be wired. Guarded by an internal `context_refreshed` flag — never fetched twice. |
| **Method** | `GET`. |
| **Query params Laura appends** | **None.** Laura requests `context_url` exactly as you supplied it — put any session selector in the URL yourself (e.g. `…/context?s=<opaque>`). |
| **Auth header** | `Authorization: Bearer <LAURA_CONTEXT_TOKEN>` (only when configured). No HMAC on this call. Follows one redirect hop, re-applying the header. |
| **Timeout** | `CALLBACK_TIMEOUT_SECONDS`, default **10 s** per attempt. Single attempt (no retry). |

**Expected response** (`200`):

```json
{
  "context": {
    "meeting": { "title": "Q3 sync", "attendees": ["…"] },
    "brief_markdown": "## Fresh brief …"
  }
}
```

- Laura reads `.context`. The refresh applies **only if `brief_markdown` is a
  string**: it replaces the live-prompt brief, and — in that same step — if
  `meeting` is also an object, updates the stored meeting metadata. A response
  carrying a fresh `meeting` but **no (or non-string) `brief_markdown` updates
  neither** (the whole refresh is gated on `brief_markdown`).
- **On error / timeout / empty / non-JSON / missing `context` / `context` not
  an object → Laura falls back** to the booking-time brief (the one from
  `context.brief_markdown` on `start`, or none) and the join proceeds. A context
  refresh failure **never** blocks the meeting.

---

## 6. `external_ref`

- **Type:** an opaque JSON object you pass on `POST /sessions/start`. Laura does
  not inspect or interpret it.
- **Stored** on the session at start (defaulting to `{}` when omitted).
- **Echoed verbatim** on **every** webhook for that session —
  `session.status`, `action.requested`, and `session.ended` all carry the exact
  object you sent. Use it to correlate a webhook back to your own record (Slack
  thread, calendar event, ticket id, …).
- **Caveat:** sessions summoned **without** `POST /sessions/start` — the Gmail
  "Add people" auto-join watcher and the calendar sync path — have no request to
  carry an `external_ref`, so their webhooks echo `external_ref: {}`. Only
  API-started sessions echo a non-empty value. (These paths still deliver
  webhooks via the deployment-wide `SURFACE_WEBHOOK_URL` default.)

---

## 7. Other endpoints (reference)

| Endpoint | Purpose |
|---|---|
| `GET /avatars` | Installed avatars, enveloped: `{"avatars": [{id, name, role, wake_words}, …]}` (array nested under `avatars` — not a bare list). **Ungated** — no auth even when a token is set. Surface `wake_words` to end users so nobody calls the wrong name in the meeting — the bot's name tile is the avatar's `name` from its `avatar.yaml`. |
| `GET /ledger?meeting_url=…` | One link's cross-meeting memory: `{meeting_key, brief, items:[…]}` (`items` are full ledger rows as in §3.5). |
| `GET /org/search?q=…&limit=…` | Ask across every meeting: `{query, ledger_matches:[…], meeting_matches:[{bot_id, saved_at, meeting_type, snippet}]}`. `limit` clamped 1–50 (default 20). Missing `q` → `400 {"error":"missing query ?q="}`. |
| `POST /sessions/{bot_id}/deliver` | Laura's **own** autopilot delivery (email + Slack) for a finished session; body `{to:[…], slack:bool}`. Orchestrated integrations normally ignore this — Cedric owns approval-gated delivery. `404` if no artifact for `bot_id`. |

---

## 8. Standalone (no orchestrator connected)

`{meeting_url}` alone (plus token) makes the default avatar join with no brief.
With no orchestrator connected Laura still senses and builds the artifact (poll
`GET …/artifact`), but there is **no autonomous execution** — she never acts.
The optional server-side `AUTOPILOT_*` notetaker can email/Slack a recap when a
meeting ends, but never executes the agreed actions. Orchestrated sessions skip
that autopilot — the orchestrator owns approval-gated delivery and all execution.

---

## 9. End-to-end example

A meeting booked through to the `session.ended` webhook.

**1) Orchestrator books the avatar.**

```http
POST /sessions/start HTTP/1.1
Host: dhfgfe6yw6.eu-central-1.awsapprunner.com
Authorization: Bearer sk_laura_live_…
Content-Type: application/json

{
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "avatar_id": "cedric",
  "join_at": "2026-07-09T15:00:00+02:00",
  "callback_url": "https://cedric.example/api/laura/events",
  "context_url":  "https://cedric.example/api/laura/context?s=sess_9f2",
  "external_ref": { "slack_thread": "C123/167.45", "cal_event": "evt_88" },
  "context": {
    "meeting": { "title": "Acme vendor onboarding", "attendees": ["Dana", "Priya"] },
    "brief_markdown": "## Goal\nComplete Acme's vendor onboarding: DPA, security review, access.\n## Open from last time\n- DPA still unsigned (owner: Dana)"
  }
}
```

**Laura →**

```json
200 OK
{
  "bot_id": "bot_b7f3a1",
  "conversation_id": "9c1a…",
  "avatar_page_url": "https://dhfgfe6yw6.eu-central-1.awsapprunner.com/talk?avatar_id=cedric&conversation_id=9c1a…&body=…",
  "scheduled_for": "2026-07-09T15:00:00+02:00"
}
```

**2) Bot joins → Laura POSTs `session.status` to `callback_url`** (signed):

```http
POST /api/laura/events HTTP/1.1
Authorization: Bearer <LAURA_WEBHOOK_TOKEN>
X-Laura-Signature: t=1752066000,v1=9a3f…hex
Content-Type: application/json

{"event":"session.status","bot_id":"bot_b7f3a1",
 "external_ref":{"slack_thread":"C123/167.45","cal_event":"evt_88"},
 "status":"live","detail":"in_call","at":"2026-07-09T13:00:05.481920+00:00"}
```

**3) Same moment — Laura GETs the fresh brief** (context refresh, once):

```http
GET /api/laura/context?s=sess_9f2 HTTP/1.1
Host: cedric.example
Authorization: Bearer <LAURA_CONTEXT_TOKEN>
```
→ `200 {"context":{"meeting":{…},"brief_markdown":"## Fresh brief …"}}` (Laura
swaps this into the live prompt; on any failure it keeps the booking-time brief).

**4) Mid-meeting someone asks the avatar to do something** → `action.requested`
(single attempt, signed): `{"event":"action.requested","bot_id":"bot_b7f3a1",
"external_ref":{…},"action":"Book the security review","owner":"Dana",
"due":"Friday","at":"…+00:00"}`.

**5) Meeting ends** (Recall terminal event, or `POST /sessions/bot_b7f3a1/end`).
Laura stops the meters, builds the artifact, and POSTs `session.ended`
(retried 5s/25s/120s until a `2xx`):

```http
POST /api/laura/events HTTP/1.1
Authorization: Bearer <LAURA_WEBHOOK_TOKEN>
X-Laura-Signature: t=1752068700,v1=c4d2…hex
Content-Type: application/json

{
  "event": "session.ended",
  "bot_id": "bot_b7f3a1",
  "external_ref": { "slack_thread": "C123/167.45", "cal_event": "evt_88" },
  "ended_at": "2026-07-09T13:45:00.002100+00:00",
  "artifact": {
    "artifact_version": 1,
    "summary": "Acme onboarding: DPA path agreed, security review to be booked, access pending approval.",
    "decisions": ["Use the standard DPA template"],
    "risks": ["Access grant blocked on security review"],
    "actions": [
      {"item":"Book the security review","owner":"Dana","deadline":"Friday","gap_type":"owner","requested_live":true},
      {"item":"Countersign the DPA","owner":"Priya","gap_type":"approval"}
    ],
    "checklist": [ /* same array as actions */ ],
    "missing_steps": ["access_provisioning_confirmed"],
    "readiness_score": 67,
    "meeting_type": "vendor_onboarding",
    "participation": [
      {"name":"Dana","lines":40,"talk_share":57,"commitments":["Book the security review"]},
      {"name":"Priya","lines":30,"talk_share":43,"commitments":["Countersign the DPA"]}
    ],
    "follow_up_email": {
      "subject": "Follow-up & open items from today's session (Cedric)",
      "body": "Hi team,\n\nOpen items from today:\n- Book the security review (owner: Dana)\n- Countersign the DPA (owner: Priya)\n\nBest,\nCedric"
    }
  }
}
```

**6) Orchestrator** verifies the signature (§2), dedupes on `bot_id` (§4.6),
correlates via `external_ref`, and drives its approval/execution flow. If the
webhook never arrived, the identical artifact is at
`GET /sessions/bot_b7f3a1/artifact` → `{"status":"done", …}`.

---

## 10. Server-side env vars

`LAURA_API_TOKEN` (inbound Bearer), `LAURA_WEBHOOK_SECRET` (HMAC key),
`LAURA_WEBHOOK_TOKEN` (outbound Bearer), `LAURA_CONTEXT_TOKEN` (context-pull
Bearer), `DEFAULT_AVATAR_ID`, `CALLBACK_TIMEOUT_SECONDS` (default 10),
`SURFACE_WEBHOOK_URL` / `SURFACE_CONTEXT_URL` (deployment-wide default
callback/context for non-API summons), `PUBLIC_BASE_URL` — see `.env.example`.
In production these live in SSM; never in git.

## 11. Known limits / roadmap

- **Single shared token.** One connected orchestrator today; a per-client key
  registry would keep this exact API shape.
- **Ephemeral store.** Ledger, artifacts, and live session state live in SQLite
  on an ephemeral App Runner disk: org memory resets on deploy, and a deploy
  **mid-meeting** loses the session (no `session.ended` fires — poll + your own
  watchdog are the backstop). The `409` dedupe is per-instance for the same
  reason. Durability (Litestream→S3 / Postgres) is pending.
- **`at`/`ended_at`** are always UTC (`+00:00`); the microsecond fraction
  appears **only when nonzero** (Python `isoformat()` omits it on an exact-second
  boundary), so parse the `.ffffff` component as optional — you may receive plain
  second precision.
