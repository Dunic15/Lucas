# Recall avatar capacity (HTTP 507) — runbook

**Symptom the user reports:** "the avatars are not entering the meetings."

**What it looks like in the logs** (App Runner application log group):

```
[recall] avatar capacity busy (507) on web_gpu/configured-transcription+voice-sep; trying next flavour
[recall] all 3 avatar flavour(s) busy (507) — retry 1/4 in 5s
INFO: POST /sessions/start HTTP/1.1" 503 Service Unavailable
```

The dashboard shows the `avatar_busy` message; no bot is ever created, so nothing
joins. Everything downstream (transcription, voice, brain) is irrelevant — the
dispatch never happened.

## Confirming it is Recall's side, not ours

`507` means Recall has no avatar-browser capacity. It is **not** caused by our
own bots holding slots — verify before escalating:

```bash
KEY=$(aws ssm get-parameter --name /laura/prod/RECALL_API_KEY --with-decryption \
      --region eu-central-1 --query Parameter.Value --output text)
curl -s -H "Authorization: Token $KEY" \
  "https://eu-central-1.recall.ai/api/v1/bot/?limit=100" \
| python3 -c "
import json,sys,collections
r=json.load(sys.stdin)['results']
c=collections.Counter((b.get('status_changes') or [{}])[-1].get('code') for b in r)
print(c)"
```

If every recent bot is `done`, we are holding zero slots and the exhaustion is
entirely on Recall's shared pool. `GET /health` → `active_sessions` should agree.

Known occurrences: **2026-07-24** (twice, cleared in minutes) and **2026-07-28**
(continuous from 11:41 UTC for 17+ minutes). Both with zero of our bots active.

## The browser flavours (`variant`)

Probed against the live API on 2026-07-28 (invalid `meeting_url` so validation
runs without ever creating a bot). Only **three** values are accepted:

| `variant` | Accepted | Used by the ladder |
|---|---|---|
| `web_gpu` | ✅ | rung 1 — preferred |
| `web_4_core` | ✅ | rung 2 — fallback |
| `web` | ✅ | equivalent to omitting `variant` (rung 3 sends no `variant`) |
| `web_2_core`, `web_8_core`, `native` | ❌ rejected | — |

**Do not lower the default to `web_4_core`.** The avatar page renders a 3D GLB
through WebGL; without a GPU it falls back to software rasterisation, so the face
is the thing that degrades. Prefer `web_gpu` for quality and let the ladder drop
to CPU only under capacity pressure — which is exactly what the 507 fall-through
is for. (The quality delta on `web_4_core` has *not* been measured yet; force the
fallback and watch the bot's camera before relying on it.)

## What the client does about it

`create_bot` builds a ladder of attempts. Every rung renders the avatar page via
`output_media`, so they all draw on avatar capacity — but they differ in browser
*flavour* (`variant`: `web_gpu`, `web_4_core`, or Recall's default), and a bigger
flavour can run dry while a smaller one still has slots.

On a 507 the client records that flavour as busy and **falls through to the next
distinct one**. Only when every flavour is dry does it back off
(`_BUSY_BACKOFF_S`) and start the round again, up to `_BUSY_RETRIES` times.

> Before 2026-07-28 a 507 restarted the ladder from rung 0, so every retry
> re-asked `web_gpu` and `web_4_core` was never reached — a busy GPU pool was
> unrecoverable even when other flavours were free.

The budget is bounded (~65s of sleep) on purpose: `create_bot` runs inside the
`/sessions/start` request, so every second is a second the dashboard spins.

## If it lasts longer than the retry budget

The client cannot fix a pool that stays dry for minutes. Escalate:

1. **Open a Recall support ticket** with the timestamps and the evidence above
   (zero of our bots active). Ask what `507` is keyed to and what the SLA is.
2. **Ask for reserved / dedicated avatar capacity.** "The shared pool ran dry"
   has an obvious fix: stop being on the shared pool. This is the highest-leverage
   mitigation and it is commercial, not engineering.
3. Meanwhile, retrying from the dashboard is safe — no bot was created, so there
   is no duplicate-bot or double-meter risk.

## Why we cannot simply degrade to "audio-only"

`output_media.camera` with `kind=webpage` is the **only** output path we use: the
avatar page is simultaneously the bot's camera *and* its microphone. Dropping
`output_media` does not yield a voice-only Laura — it yields a mute one. Any
graceful-degradation work has to establish a separate Recall audio-output path
first; it is not a config flag.
