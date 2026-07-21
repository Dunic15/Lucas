# Cedric ⇄ Laura dashboard bridge: implementation spec for the Cedric service

**Audience:** whoever owns the Cedric orchestrator (the `SFF-Studio/Cedric`
repo; the service that today runs entirely in Slack).
**Goal:** let a user drive the **same** Cedric from the Laura dashboard
(chat with it, and approve meeting actions that Cedric then executes through
its connectors); without Slack.

**Status of the two sides**

- **Laura side (this repo): already built.** The dashboard chat tab, the
  Action Center, the approve doors, the client that dispatches approved
  actions to Cedric, and the receipts/status rendering all exist and ship in
  production. Laura already *calls* every endpoint below and already *serves*
  every callback below.
- **Cedric side: to build.** Cedric must expose an HTTP front door alongside
  its Slack front door; three inbound receivers, and two outbound callbacks
  into Laura. That is the entirety of the work in this document.

Nothing here requires further Laura changes. The moment Cedric answers these
endpoints, the dashboard lights up.

---

## 0. Base URLs & config

Laura is configured with **one** setting that points at Cedric:

```
CEDRIC_ORGS_URL = https://<cedric-host>/api/laura/orgs
```

Laura derives the other two Cedric endpoints from it by string-replacing the
trailing `/api/laura/orgs`:

| Purpose | Method + path (Cedric serves) |
|---|---|
| Workspace provisioning | `POST /api/laura/orgs` |
| **Action execution** | `POST /api/laura/actions` |
| **Events (incl. chat)** | `POST /api/laura/events` |

So **all three must live under the same origin/prefix.**

Cedric, in turn, needs Laura's base URL to call back:

```
LAURA_BASE = https://dhfgfe6yw6.eu-central-1.awsapprunner.com   (prod)
```

Cedric calls back into `POST {LAURA_BASE}/org/chat` and
`POST {LAURA_BASE}/org/actions/{action_id}/status`: both already live.

---

## 1. Auth (both directions)

There are two credential pairs, established at provisioning time (§2).

### 1a. Laura → Cedric  (Cedric verifies)

Every request Laura sends to Cedric carries:

- **`Authorization: Bearer <webhook_token>`**: the `webhook_token` Cedric
  minted for this org at provisioning (or the deployment-wide
  `LAURA_WEBHOOK_TOKEN` for the demo org only).
- **`X-Laura-Signature: t=<unix_ts>,v1=<hex>`** where
  `hex = HMAC_SHA256(key = <webhook_secret>, msg = "<t>." + <raw_request_body>)`.
  `<webhook_secret>` is the secret Cedric minted for this org at provisioning.
  **Cedric must verify this signature** and reject on mismatch. Reject stale
  `t` (suggest ±5 min) to stop replay.
- **`Idempotency-Key: <action_id>`** on `/api/laura/actions` (see §3).

`Content-Type: application/json`. Bodies are signed **raw**: verify against
the exact received bytes, not a re-serialization.

### 1b. Cedric → Laura  (Laura verifies)

Every callback Cedric sends to Laura carries:

- **`Authorization: Bearer <laura_org_token>`**: a per-org bearer **Laura**
  issues for this workspace. Laura resolves it to the org and scopes the write
  to that org only. (The deployment-wide `LAURA_API_TOKEN`, if shared, resolves
  to the demo org; fine for a single-tenant pilot, not for multi-tenant.)

> **⚠ One handshake detail to finalize:** today Laura's provisioning call (§2)
> sends Cedric the org identity but does **not** yet hand Cedric a
> `laura_org_token` for the callback direction. Pick one:
> **(a)** Laura includes a freshly minted per-org bearer in the provisioning
> request body (recommended; one round trip; Laura change is ~5 lines), or
> **(b)** for a single pilot org, share one `LAURA_API_TOKEN` out of band and
> Cedric uses it for all callbacks. Tell me which and I'll wire Laura's side
> for (a).

---

## 2. Provisioning handshake  (`POST /api/laura/orgs`): prerequisite

When a workspace connects Cedric as its "brain", Laura POSTs:

```
POST /api/laura/orgs        (or /api/laura/orgs/pending when team_id is unknown)
Authorization: Bearer <CEDRIC_ORGS_TOKEN>     # a shared secret you give us
Content-Type: application/json

{ "org_id": "<laura_org_id>",
  "team_id": "<slack_team_id or null>",
  "default_slack_channel": "<channel or ''>",
  "avatar_id": "cedric" }
```

Cedric responds `2xx` with the per-org credentials Laura will use for §1a:

```json
{ "credentials": { "webhook_secret": "<hmac secret>",
                   "webhook_token":  "<bearer>" } }
```

Store these against `org_id`. They are how each workspace gets isolated
credentials without rotating anyone else's.

---

## 3. Action execution  (`POST /api/laura/actions`): **the core**

When a user clicks **Approve** on a meeting action in the dashboard, Laura
sends exactly this (already implemented, `cedric/callback.dispatch_action`):

```
POST /api/laura/actions
Authorization: Bearer <webhook_token>
X-Laura-Signature: t=...,v1=...
Idempotency-Key: <action_id>
Content-Type: application/json

{ "org_id": "<org>",
  "action": {
    "action_id": "<aid>",
    "tenant": { "org_id": "<org>", "team_id": "<slack_team>" },
    "type": "email.send",
    "args": { "to": ["marco@acme.com"], "subject": "Recap", "body": "…" },
    "execution_route": "cedric",
    "approval_mode": "pre_approved",
    "approved_by": "<laura_user_id>",
    "idempotency_key": "<aid>",
    "correlation_id": "<aid>" } }
```

**`approval_mode: "pre_approved"` means the human already approved in the
dashboard: Cedric must NOT re-ask.** Just execute.

### `type` + `args` shapes

| `type` | `args` |
|---|---|
| `email.send` | `{ to: [string], subject, body }` |
| `calendar.create_event` | `{ title, start, end, attendees?: [email] }` |
| `asana.create_task` | `{ name, notes?, project?, assignee?, due_on? }` |
| `asana.update_task` | `{ task: "<gid>", completed?, due_on?, … }` |
| `asana.add_comment` | `{ task: "<gid>", text }` |
| `task.freeform` | `{ item, owner }`: untyped ask; Cedric's agent interprets it |

`task.freeform` is the common case today (items Laura couldn't type). Cedric's
agent should reason over `item`/`owner` and pick the connector, exactly as it
would from a Slack request.

### Response contract (Laura already branches on these)

| HTTP | Laura's interpretation |
|---|---|
| `2xx` | **Accepted** (not necessarily done). Row shows "approved · with Cedric"; the real receipt arrives via §5. |
| `404` / `405` | "Receiver isn't built yet." Row now shows **Couldn't run** with that reason. |
| `422` | "Unsupported action type." Surfaced to the user. |
| other | Surfaced as an execution error. |

`2xx` is an **acknowledgement of acceptance**, returned fast. Do the actual
work async, then report terminal status via §5. Idempotency: two dispatches
with the same `Idempotency-Key`/`action_id` must execute **once**.

---

## 4. Chat bridge: inbound half  (`POST /api/laura/events`)

When a user types in the dashboard Cedric tab, Laura sends:

```
POST /api/laura/events
Authorization: Bearer <webhook_token>
X-Laura-Signature: t=...,v1=...

{ "event": "chat.message",
  "org_id": "<org>",
  "message_id": 42,
  "text": "Cedric, chase the DPA with legal",
  "sender": "Ada (ada@acme.com)",
  "event_id": "<uuid>",
  "at": "2026-07-20T09:00:00Z" }
```

Cedric processes it with its normal brain and **replies via §5a**. Respond
`2xx {"ok": true}` to acknowledge receipt (Laura reads nothing else back).

The same door also receives `action.approved` and `action.status` envelopes
(Laura mirroring native-route outcomes). Cedric may ignore these; if it acts on
them, dedupe on `event_id` to avoid echo loops.

---

## 5. Callbacks INTO Laura (already live: Cedric calls these)

### 5a. Post a chat reply  (`POST {LAURA_BASE}/org/chat`)

```
POST /org/chat
Authorization: Bearer <laura_org_token>

# a text reply:
{ "message": { "text": "On it. I'll chase legal and confirm here.",
               "sender_label": "Cedric" } }

# …or an action card the user can Approve/Reject inline in the thread:
{ "action_card": { "action_id": "<aid>", "item": "Send the recap",
                   "owner": "Dana", "due": "Friday",
                   "note": "One thing needs your sign-off:" } }
```

Send exactly one of `message` | `action_card`. The message appears in the
dashboard chat within its 4-second poll. **Distilled content only; never raw
meeting-transcript text.**

### 5b. Report action status  (`POST {LAURA_BASE}/org/actions/{action_id}/status`)

After executing a dispatched action (§3), report the outcome:

```
POST /org/actions/<aid>/status
Authorization: Bearer <laura_org_token>

{ "status": "done",
  "detail": "Sent; https://mail.google.com/…/<msgid>" }
```

`status` ∈ `{ executing, done, failed, rejected, needs_details, proposed,
approved }` (terminal: `done` / `failed` / `rejected`). Put the receipt
link/text in `detail`: the dashboard renders it as the row's receipt
(`done` becomes a "Done ↗" link when `detail` contains a URL). This closes the
loop: the Action Center row flips from "approved · with Cedric" to the final
receipt.

---

## 6. End-to-end flow (what the user sees once this exists)

1. Meeting ends → Laura extracts action points → Action Center.
2. User clicks **Approve** → Laura `POST /api/laura/actions` (§3).
3. Cedric `2xx` → row shows "approved · with Cedric".
4. Cedric executes through its connector → `POST /org/actions/{id}/status`
   `done` + receipt (§5b) → row shows the receipt.
5. Separately, user chats in the Cedric tab → `chat.message` (§4) → Cedric
   replies via `POST /org/chat` (§5a) → appears in the thread.

---

## 7. Acceptance checklist for the Cedric side

- [ ] `POST /api/laura/orgs` mints + returns `{webhook_secret, webhook_token}`.
- [ ] Verifies `X-Laura-Signature` (HMAC over `"<t>." + raw_body`) on every
      inbound call; rejects bad sig and stale `t`.
- [ ] `POST /api/laura/actions`: idempotent on `action_id`, treats
      `pre_approved` as "don't re-ask", executes, returns fast `2xx`.
- [ ] Reports terminal status to `/org/actions/{id}/status` for every dispatch.
- [ ] `POST /api/laura/events` handles `chat.message`; replies via `/org/chat`.
- [ ] All callbacks send `Authorization: Bearer <laura_org_token>` (see the §1b
      handshake note).

---

## 8. What's stubbed on Laura today (so you know the seam)

Until Cedric answers §3/§4, the dashboard Cedric tab is served by a **built-in
stand-in** (`CEDRIC_CHAT_NATIVE_REPLY`, `backend/app/cedric/chat_responder.py`)
-- Laura's own model answering *as* Cedric, grounded in org state, so the tab
isn't dead. **Set `CEDRIC_CHAT_NATIVE_REPLY=false` once Cedric's chat bridge is
live**, and the dashboard hands chat to the real Cedric instead. Action
dispatch (§3) already prefers Cedric whenever the receiver answers `2xx`; no
flag needed there.
