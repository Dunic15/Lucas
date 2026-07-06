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

# 2. launch (one command — everything is in the template)
aws ec2 run-instances --launch-template LaunchTemplateName=laura-gpu \
  --region eu-central-1

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
# 8. STOP THE BOX after testing: aws ec2 stop-instances ... (GPU idle = money;
#    same golden rule as the Recall meter)
```

## Cost
g5.xlarge $1.006/hr eu-central-1 → **~$0.017/min of meeting**, $0 stopped.
(Keyframe Labs, the commercial alternative: $0.06/min, zero ops — the fallback
if this track stalls.)

## Protocol (page <-> GPU server)
One websocket `/stream`: client sends `{"type":"speak","audio_b64":<mp3>}`;
server sends `hello/talk_start/talk_end` JSON text frames + continuous binary
JPEG frames (idle loop when silent, lip-synced frames while talking). The page
starts audio playback on `talk_start` so mouth and sound line up.
