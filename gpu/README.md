# Laura photoreal avatar — GPU track (Stage 2)

Turns Laura photorealistic: a GPU EC2 box runs **MuseTalk** (open source) to
lip-sync a real-looking reference face to her TTS audio, streamed into
`frontend/photoreal.html`, which Recall renders as the bot camera. Brain, TTS,
and all meeting logic stay on the App Runner backend — this box ONLY renders
the face, so it runs (and bills) only during meetings.

## Status / what's already prepared (2026-07-06)

| Piece | State |
|---|---|
| GPU quota (the blocker) | **Requested** (8 vCPUs, req id `5e206c5f…AiMv1Gxw`) — status was `CASE_OPENED` |
| Security group | `sg-0515115a1c3a13a9d` (8080 open) |
| IAM role/profile | `laura-gpu-role` / `laura-gpu-profile` (SSM-managed, no SSH) |
| Launch template | **`laura-gpu`** (`lt-03496053f2de60222`): DLAMI Ubuntu 22.04 + driver, g5.xlarge, 120GB gp3 |
| Streaming server | `gpu/server.py` — stub engine tested end-to-end; MuseTalk engine seam ready |
| Page | `frontend/photoreal.html` — same speak contract as talk.html |
| Reference face | `gpu/assets/reference.jpg` = PLACEHOLDER (3D render). Swap for an AI-generated photoreal portrait (no real person → no likeness issues) |

## Launch day (when the quota is APPROVED)

```bash
# 1. check quota
aws service-quotas get-requested-service-quota-change \
  --request-id 5e206c5f0626428fbed4f482718bd135AiMv1Gxw --region eu-central-1

# 2. launch: creates the instance from the template, arms the 90-min TTL,
#    health-checks, prints the stream URL (exit != 0 means not healthy)
./launch.sh

# 3. push code via SSM (repo is private; no tokens on the box):
#    - mkdir /opt/laura-gpu + copy gpu/server.py, gpu/assets/, gpu/setup.sh
#      (aws ssm send-command with AWS-RunShellScript, files inlined/base64)
# 4. MODE=stub bash setup.sh          -> verify the transport end-to-end first
# 5. MODE=musetalk bash setup.sh      -> weights download + real engine
#    wire musetalk_adapter.py (thin wrapper over MuseTalk realtime inference —
#    expect a tuning session: chunk size, fps, face-crop box)
# 6. TLS for the browser: Cloudflare DNS record gpu.lauravatar.com (proxied)
#    -> wss://gpu.lauravatar.com/stream terminates TLS at Cloudflare, ws to :8080
# 7. backend env: GPU_STREAM_URL=wss://gpu.lauravatar.com/stream, then
#    AVATAR_PAGE=photoreal to flip meetings onto it (talk.html stays fallback)
# 8. done testing?  ./stop.sh   (GPU idle = money; same golden rule as the
#    Recall meter — but see Cost controls: the box also stops itself)
```

## Cost controls (issue #3) — the box must NEVER be always-on

The GPU runs **only during a meeting or demo window**. Four layers make sure of
it — each one alone is enough to stop the meter:

| Layer | What | When it fires |
|---|---|---|
| `./launch.sh` TTL | arms `shutdown -h +N` on the box (`GPU_TTL_MINUTES`, default 90) | hard cap per window, survives page/meeting/laptop crashes |
| Boot TTL (`laura-gpu-ttl.service`, from setup.sh) | every boot arms a 90-min auto-stop | catches boxes started by ANY path (backend, console) |
| Idle watchdog (`server.py`) | no page connected for `GPU_IDLE_SHUTDOWN_MINUTES` (10 on the box) → self-stop | meeting ended / never started |
| Meeting-bound runtime (backend `gpu_runtime.py`) | session starts → `ec2 start`; last session ends → `ec2 stop` after `GPU_IDLE_STOP_MINUTES` (10) grace | the normal path: GPU exists only around meetings |

```bash
./launch.sh    # start the window: boot box, arm TTL, health-check, print ws URL
./status.sh    # is it burning money? state, uptime, est. cost, stream health
./stop.sh      # end the window now (--terminate to also delete the volume)
```

**Meeting-bound runtime setup (one-time):** set backend env
`GPU_INSTANCE_ID=<i-...>` (with `AVATAR_PAGE=photoreal`) and attach the
scoped start/stop policy to the App Runner instance role:

```bash
aws iam put-role-policy --role-name LauraAppRunnerInstanceRole \
  --policy-name laura-gpu-control \
  --policy-document file://gpu/iam-backend-gpu-policy.json
```

Keep the box **stopped, not terminated** — autostart can only wake a stopped
instance. Boot ~90s: calendar-scheduled joins hide it; instant Gmail joins run
on the static portrait until the stream connects (fallbacks below).

**Budget safety net (recommended):** a monthly EC2 budget alert at ~$50 —
about 50 GPU-hours, far beyond normal meeting use — emails you if some path
above ever fails:

```bash
aws budgets create-budget --account-id 836739852304 \
  --budget '{"BudgetName":"laura-gpu-monthly","BudgetLimit":{"Amount":"50","Unit":"USD"},
             "TimeUnit":"MONTHLY","BudgetType":"COST",
             "CostFilters":{"Service":["Amazon Elastic Compute Cloud - Compute"]}}' \
  --notifications-with-subscribers '[{"Notification":{"NotificationType":"ACTUAL",
      "ComparisonOperator":"GREATER_THAN","Threshold":80},
      "Subscribers":[{"SubscriptionType":"EMAIL","Address":"duccio.profeti@gmail.com"}]}]'
```

The boot TTL doubles as the daily-runtime guard: one boot can never exceed its
TTL, so runaway cost requires something restarting the box in a loop — which
the budget alert catches.

**Safe metrics only:** `GET :8080/metrics` → uptime, actual FPS, first-frame
latency, frames sent, clients, estimated cost. `/tts` responses carry `tts_ms`.
No transcript text or user content is ever logged or exported (PII rule).

## Fallback order (page never goes dark)

1. GPU stream up → **photoreal avatar** (MuseTalk frames)
2. GPU unreachable → **static portrait** (`/laura-reference.jpg`, audio still plays; page retries the stream every 3s and upgrades when it connects)
3. Static portrait also unavailable → page redirects to **`/talk`** (3D TalkingHead avatar, same conversation params)

## Cost
g5.xlarge $1.006/hr eu-central-1 → **~$0.017/min of meeting**, $0 stopped.
(Keyframe Labs, the commercial alternative: $0.06/min, zero ops — the fallback
if this track stalls.)

## Protocol (page <-> GPU server)
One websocket `/stream`: client sends `{"type":"speak","audio_b64":<mp3>}`;
server sends `hello/talk_start/talk_end` JSON text frames + continuous binary
JPEG frames (idle loop when silent, lip-synced frames while talking). The page
starts audio playback on `talk_start` so mouth and sound line up.
