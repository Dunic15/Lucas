# Laura Latency Runbook

This runbook keeps latency work separate from the live streaming implementation.
Use it to verify that AWS App Runner, Recall, the backend, the LLM path, the
websocket, and Anam are improving for the right reason.

## Current Priority

1. Keep AWS App Runner in `eu-central-1` near Recall's configured API region.
2. Keep the live-answer path on Haiku unless quality is clearly insufficient.
3. Measure each hop before tuning anything else.
4. Keep live answers short and streamed; use the higher-quality path for
   post-meeting artifacts.

## AWS App Runner Configuration

Current live backend:

- URL: `https://dhfgfe6yw6.eu-central-1.awsapprunner.com`
- Region: `eu-central-1`
- Instance: `1 vCPU / 3 GB RAM`
- Autoscaling: `LauraCostControl`, `min 1 / max 1`
- `PUBLIC_BASE_URL=https://dhfgfe6yw6.eu-central-1.awsapprunner.com`
- `BRAIN_MODEL=claude-haiku-4-5-20251001`
- `RECALL_API_BASE=https://eu-central-1.recall.ai`

App Runner does not scale to zero automatically. Pause it when testing is done,
and resume it before using the AWS `/join` page, Recall webhooks, or the Chrome
extension.

## Render Fallback Configuration

The Render Blueprint remains as a fallback and now uses the same live model:

- `plan: starter`
- `healthCheckPath: /health`
- `BRAIN_MODEL=claude-haiku-4-5-20251001`
- `RECALL_API_BASE=https://eu-central-1.recall.ai`

## Probe Commands

Safe health probe:

```bash
python scripts/latency_probe.py https://dhfgfe6yw6.eu-central-1.awsapprunner.com --iterations 5
```

Confirm the live Render service plan after authenticating the CLI:

```bash
render login
render services --output json
```

Backend + RAG + full LLM answer probe:

```bash
python scripts/latency_probe.py https://dhfgfe6yw6.eu-central-1.awsapprunner.com --iterations 3 --include-ask
```

Real session-start probe. This sends a Recall bot into the meeting:

```bash
python scripts/latency_probe.py https://dhfgfe6yw6.eu-central-1.awsapprunner.com \
  --meeting-url https://meet.google.com/xxx-yyyy-zzz
```

Run the health probe in three situations:

- immediately after deploy
- after 45-60 minutes idle
- during a live meeting test

## Current AWS Baseline

Measured from this workspace on 2026-07-02 with three iterations:

```text
GET /health: 30-264 ms, median 31 ms
POST /demo/ask: 1445-2293 ms, median 1669 ms
```

Interpretation: the deployed AWS backend is warm and healthy. The remaining
latency is dominated by the answer path rather than hosting cold start.

## Manual Live Test

For one live test, record these timestamps in Render logs or browser devtools:

- `meeting_utterance_finalized`: Recall transcript webhook arrives.
- `wake_gate_passed`: wake word accepted and cooldown passed.
- `rag_done`: retrieval finished.
- `llm_first_token`: first streamed token arrives.
- `first_sentence_sent`: first websocket `speak` chunk sent.
- `avatar_ws_received`: `avatar.html` receives the chunk.
- `anam_talk_called`: frontend calls Anam `talk()`.
- `audible_speech_start`: human-observed start of speech.
- `llm_done`: stream completed.

The most important perceived-latency number is:

```text
audible_speech_start - meeting_utterance_finalized
```

## Target Numbers

Use these as practical targets, not hard guarantees:

- Warm `/health`: p50 under 500 ms from Europe.
- Idle `/health`: no 30-60 second cold start while App Runner is running.
- `/demo/ask`: p50 under 2500 ms for a short live-style answer.
- First spoken chunk after Recall finalizes the utterance: under 2000 ms.
- Full spoken answer: under 5000 ms.

If the first spoken chunk is still slow after streaming, check in this order:

1. Recall finalization delay, which is outside this backend.
2. AWS/Recall region mismatch.
3. LLM first-token latency.
4. Sentence buffering thresholds.
5. Anam `talk()` startup and TTS latency.

## Review Checklist For Streaming Changes

- The old confidence gate must not be silently weakened. If streaming uses a
  sentinel such as `SKIP`, test that insufficient context produces silence.
- The frontend should queue chunks so multiple `talk()` calls do not overlap.
- The backend should never send token-by-token speech to Anam; send sentence or
  natural phrase chunks.
- A websocket disconnect should not crash the Recall webhook.
- The live path should use the fast model; post-meeting summaries can use the
  quality model.
- Log enough timing data to tell first-token latency from TTS latency.
