# Cedric — Tools, Connectors, and the Capture-Approve-Execute Loop

**Process owner:** Ben (Cedric agent + integrations)
**Applies to:** What Cedric can look up live, what he queues, and how a
queued action turns into a completed action
**Last reviewed:** 2026-07-13

## The core loop: capture, approve, execute

Cedric never takes a side-effecting action DURING a meeting. When someone
asks him to do something — send, schedule, create, update, remind — he
captures it on the spot with `queue_action` (the task, the requester, the
best-guess owner, any deadline) and says out loud that he'll take care of it
once the call wraps. He never claims a task is already done live. After the
meeting ends, every captured action becomes one Approve/Reject card in the
Slack channel the meeting came from. On Approve, the matching connector runs
and the result (sent, created, updated, or failed) posts back to the same
thread. On Reject, nothing runs. This is one shared memory: the meeting half
captures and agrees, the Slack half executes — the same Cedric on both
sides of the call.

## What Cedric can use live, in the meeting

A small set of tools are safe to use WHILE the meeting is running because
they only read, they don't change anything:

- **`gmail_search` / `gmail_get_thread`** — look up whether an email or
  thread already exists before promising to send a new one ("did we already
  reply to them?"). Read-only lookup, not a send.
- **`calendar_list_events`** — check what's already on a calendar before
  proposing a new time, so Cedric doesn't suggest a slot that's already
  booked.
- **`web_search`** — anything current or factual that isn't in his brief or
  knowledge docs: news, prices, a company, a person. Cedric looks it up
  rather than guessing, and says so when he does.
- **`list_connectors`** — check which tools are actually connected for this
  workspace before promising an action that needs one ("are we connected to
  Linear yet?").
- **`report_error`** — if a live lookup fails (a search errors, a connector
  is unreachable), Cedric surfaces that plainly ("that lookup didn't come
  back — I'll flag it rather than guess") instead of going silent or making
  something up.

**`gmail_send` and `calendar_create_event` are never called live.** Sending
an email or creating a calendar event is a side effect with a real-world
consequence, so both always go through `queue_action` first and only run
after a human approves the card in Slack — same as every other connector
action below.

## Connectable tools (the Slack side does the work)

Each of these connects once per workspace via OAuth (see "The connect flow"
below), then Cedric can act on it from either half — captured in the
meeting, executed from Slack after approval:

- **gmail** — search the inbox, summarize a thread, draft and send replies.
- **google_calendar** — list events, find a free slot, create or update an
  event (with a Meet link when relevant).
- **slack** — post messages, summarize a channel. This is also the channel
  the team already uses to talk to Cedric, so it's connected by default.
- **stripe** — pull a revenue report, list customers, create an invoice.
- **hubspot** — update a CRM record, pull a pipeline report, log an
  activity.
- **linear** — create an issue, pull a sprint summary, update a status.
- **github** — summarize open PRs, list open issues, summarize review
  activity.
- **notion** — draft a doc, update a database, search existing pages.
- **laura** — the meetings connector itself: joining calls, capturing
  actions, and building the post-meeting artifact — this is how Cedric gets
  into the room in the first place.

Beyond that shortlist, the broader integration directory includes Microsoft
Teams, Zoom, Discord, Salesforce, Pipedrive, Attio, Apollo, and QuickBooks —
connectable per workspace the same way. Which of these are actually
connected varies by workspace; Cedric checks with `list_connectors` rather
than assuming, and says plainly when something asked for isn't connected
yet.

## The connect flow

If a request needs a tool that isn't connected, Cedric doesn't fail
silently and he doesn't pretend to do the work anyway. He says the tool
isn't connected yet and, when useful, gets a `get_connect_link` — a one-time
OAuth link a team member opens to authorize that tool for the workspace.
Once connected, everyone in the workspace benefits — Cedric doesn't
reconnect per person or per meeting.

## What "connected" actually means live vs. after the call

In the meeting, "connected" only unlocks the read-only lookups above
(`gmail_search`, `calendar_list_events`, `list_connectors`) — it does not
mean Cedric starts sending or creating things mid-call. Every write action,
connected tool or not, still goes through the same capture-approve-execute
loop. Being connected changes what happens on approval (a real send/create
vs. a "not connected yet" card); it never changes what Cedric does live.

## Owners

- Ben owns the Cedric agent, its tool wiring, and which connectors are live
  for a given workspace.
- Each connected tool (Stripe, HubSpot, Linear, etc.) is owned in Slack by
  whoever authorized it via `get_connect_link` — Cedric doesn't self-connect
  new tools.
- The requester in the meeting owns confirming the task is worth queuing;
  the named owner on the action owns approving or rejecting the card.

## Definition of done

A requested action is "done" only when its Slack Approve/Reject card has
been approved AND the connector call it triggered has completed and posted
its result back to the thread — not when Cedric merely captured it in the
meeting, and not when the card is sitting unapproved.

## Common gaps

- Treating "connected" as "already done" — a connected tool still needs the
  Slack approval before anything runs.
- Asking Cedric to do something with a tool nobody has connected yet, and
  assuming it happened because he confirmed the request out loud.
- Approving a card without checking the captured owner is correct — a
  misheard "you" in a group call can attach a task to the wrong person.
- Expecting a live send or calendar write mid-meeting; that only happens
  after the call, on approval.
