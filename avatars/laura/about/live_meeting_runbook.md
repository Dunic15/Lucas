# Live Meeting Runbook

**Owner:** Laura operations
**Applies to:** Sending Laura into Google Meet, Zoom, or Microsoft Teams
**Last reviewed:** 2026-07-20

## Starting a live session

Laura can be sent into a meeting from the manual `/join` page, the Google Meet
Chrome extension, the calendar/Gmail watcher, or the direct API route
`POST /sessions/start`.

The backend must be publicly reachable because Recall.ai needs to reach two
things:

- the webhook for live transcript and bot status events
- the avatar page URL that Recall renders into the meeting

For local or CSCS cluster tests, run the backend on `127.0.0.1:8000` and expose
it with a public HTTPS tunnel such as cloudflared. After the tunnel starts, set
the public base URL and Recall webhook to the tunnel domain:

`https://<tunnel-domain>/webhooks/recall`

The callback and webhook URLs must use HTTPS, not localhost.

## Recall region and auth checks

Use the Recall status endpoint to verify region and key configuration:

`/recall/status?check_auth=true`

If the response says Recall rejected the key with 401, check both the key and
the region. This project uses the EU Recall base:

`https://eu-central-1.recall.ai`

Do not confuse the Recall API key with the webhook signing secret. A `whsec_...`
value is a webhook secret, not a Recall API key.

## When Laura does not arrive

If the bot does not join the meeting, check in this order:

1. The backend is running and reachable at the public base URL.
2. The meeting URL is valid and supported by Recall.
3. `RECALL_API_BASE` matches the region of the Recall API key.
4. `/recall/status?check_auth=true` returns ready/auth valid.
5. The Gmail/calendar watcher saw the invite if using auto-join.
6. Recall bot creation did not return a 400 or 401.

If using a temporary cloudflared tunnel, update every URL that depends on it
after each new tunnel is created. Quick tunnels change domains.

## When Laura joins but does not speak

Separate answer generation from speech delivery.

If backend logs show answer generation or latency lines, Laura's brain produced
text. The remaining problem is avatar delivery or speech playback.

Known App Runner behavior: this service has seen WebSocket upgrade requests
return HTTP 403. The avatar page therefore also uses HTTP polling for queued
messages. If speech fails while answers are generated, inspect the avatar page
message polling and browser console, not the RAG index.

## Recall bot variants

The preferred high-performance Recall output media variant is `web_gpu`.
It provides more CPU and memory and supports WebGL. The fallback is
`web_4_core`, then the default web variant if Recall rejects the premium one.

All Recall output media variants still output at the same fixed meeting camera
resolution and frame rate: 1280x720 at 15 fps. `web_gpu` can reduce dropped
frames or resource pressure, but it does not increase the meeting platform's
resolution or remove platform compression.

## Duplicate bots after deploys

App Runner may overlap old and new instances during deploys. If both instances
process the same Gmail/calendar trigger, duplicate bots can appear. The newer
code should prefer the higher-performance Recall bot variant when reconciling
duplicates.

The immediate operational fix is to stop deploying, ensure no Laura bot remains
in a meeting, wait for old instances to drain, then test one fresh meeting.

## Ending a session

Always end live sessions when testing is complete. Use `/sessions/{id}/end` with
the bot/session id from `/sessions/start`. This stops the Recall bot and avoids
unnecessary per-minute billing.

## Browser tasks (Sable operator)

Laura's supervised web browsing runs through the browser operator, not
through the meeting pipeline. Tasks are driven from the dashboard (mission
input + Approve button) or the org API: a goal opens a bounded Browserbase
session, Claude plans one step at a time from real screenshots, a
deterministic policy re-checks every step, and consequential operations
stop at an approval door. Defaults: read-only (`BROWSER_ALLOW_WRITES`
off), navigation limited to `BROWSER_ALLOWED_DOMAINS`, hard caps on steps,
duration, and model calls. Every session is recorded and replayable in the
Browserbase dashboard.

Saved logins (browser identities): an org can connect a site once — a
human logs in through the token-gated live view, credentials go straight
from their keyboard to the site, and the provider persists the session
encrypted. Later browse tasks attach that identity by label
(`identity_label` on session create) and wake up already signed in, still
read-only and policy-checked. Revoking the identity from the API cuts
future access without touching the site password.

Operationally: browser sessions are separate from meeting sessions — they
do not touch the Recall per-minute meter, and they close themselves at the
task boundary. Speech in a meeting does not yet trigger a browse directly,
and the meeting-tile live view from the Sable design is not wired yet;
today the flow is capture → typed action → approve → execute.

## Common questions

**"Why is the webhook URL `/webhooks/recall`?"**
Recall posts bot and transcript events to that backend route. The base domain
changes when the public tunnel changes.

**"Can web_gpu make the avatar higher resolution?"**
No. It gives more compute and WebGL, but meeting output remains 1280x720 at
15 fps.

**"Why did it work before and then not arrive?"**
Check whether the tunnel domain changed, whether the Recall key and region still
match, and whether the bot creation request failed.
