# laura-ears: Cloudflare Worker relay for Gemini ears

Why this exists: **AWS App Runner (the Laura backend) does not accept inbound
WebSockets**, so Recall's real-time audio (`audio_mixed_raw`) can't reach it
directly (rejected with 403 at the edge). Cloudflare Workers DO accept inbound
WS and can open an outbound WS to Gemini Live. This relay bridges them:

```
Recall --audio(WS)--> laura-ears (this worker) --Gemini Live(WS)--> turns
                          └── HTTP POST /webhooks/recall ──> backend (App Runner)
```

The relay is deliberately dumb; the backend owns config, auth, and the whole
meeting pipeline (gates, brain, ElevenLabs voice). Per session the relay:
1. `GET {BACKEND_URL}/internal/ears-config/{cap}` (bearer) → mode/model/persona
   /bot_id + a fresh Vertex token (the service account never leaves the backend).
2. Opens the Gemini Live WS, streams the meeting audio in (PCM 16 kHz base64).
3. On each completed turn, POSTs a synthesized `transcript.data` (marker
   `laura_ears`, + the draft reply in reply mode) to `/webhooks/recall?cap=`.

## Deploy (via the Cloudflare API: the MCP tools return opaque output)
Needs a CF API token with **Workers Scripts: Edit** (account `8ff2d56d...`).
```bash
# subdomain (one-time): PUT .../workers/subdomain {"subdomain":"lauravatar"}
curl -X PUT "https://api.cloudflare.com/client/v4/accounts/$CF_ACCT/workers/scripts/laura-ears" \
  -H "Authorization: Bearer $CF_TOKEN" \
  -F 'metadata={"main_module":"worker.js","compatibility_date":"2026-06-01","observability":{"enabled":true}};type=application/json' \
  -F "worker.js=@relay/laura-ears/worker.js;type=application/javascript+module"
# enable route: POST .../workers/scripts/laura-ears/subdomain {"enabled":true}
# secrets: PUT .../workers/scripts/laura-ears/secrets {name,text,type:"secret_text"}
#   BACKEND_URL     = https://<app-runner-host>
#   BACKEND_BEARER  = LAURA_API_TOKEN
```
URL: `wss://laura-ears.lauravatar.workers.dev/realtime/recall-audio/<cap>`
Backend must have `EARS_RELAY_WS_BASE=wss://laura-ears.lauravatar.workers.dev`
and `GEMINI_EARS_MODE=reply` (or `on`/`shadow`).

## Gotchas learned the hard way
- Gemini sends **binary** WS frames: decode Blob/ArrayBuffer with TextDecoder,
  not a naive string cast (a throwing decode silently ate every message).
- A plain Worker is torn down after `fetch()` returns: keep the session alive
  with `ctx.waitUntil(handleSession(...))` or the audio pumps die.
- Logs are **PII-safe**: counts and message *types* only, never transcript text.
