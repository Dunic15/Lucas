# Gemini via Vertex AI — setup

Adds Google **Gemini** as a brain provider (`BRAIN_PROVIDER=vertex`) and documents
the **realtime voice** path (Gemini Live). The point of going through **Vertex AI**
rather than the AI Studio Gemini API: Vertex bills to **Google Cloud**, so it can
run on **GCP credits** (a Free-Trial credit covers Vertex AI but **not** the AI
Studio Gemini API — that's why an AI Studio key hits "prepay depleted" even with
credit sitting in the project).

The live meeting path is **unchanged** — prod still runs Cerebras (live) + Claude
Sonnet (post). This is opt-in and off by default.

Verified end-to-end 2026-07-14 on project `868562221752` (region `us-central1`,
account `laura.ai.122222@gmail.com`).

## What's available on Vertex (verified, this project)

| Role | Model | Endpoint |
|---|---|---|
| Brain | `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-2.5-pro` | region **and** `global` |
| Brain | `gemini-3.5-flash`, `gemini-flash-latest` | **`global` only** |
| Voice (Live) | `gemini-live-2.5-flash` | **`global` websocket host only** |

Not present on this project: `gemini-3-flash`, `gemini-3-pro`, `gemini-2.0-*`,
the `*-native-audio-*` Live variants.

## 1. Enable + auth

```bash
gcloud services enable aiplatform.googleapis.com --project 868562221752

# Service account for the app (role: Vertex AI User)
gcloud iam service-accounts create laura-vertex --display-name="Laura Vertex"
gcloud projects add-iam-policy-binding 868562221752 \
  --member="serviceAccount:laura-vertex@868562221752.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"
gcloud iam service-accounts keys create ~/laura-vertex-sa.json \
  --iam-account=laura-vertex@868562221752.iam.gserviceaccount.com
```

Point the app at the key (never commit it — env/SSM only):

```bash
export GOOGLE_APPLICATION_CREDENTIALS=~/laura-vertex-sa.json
```

`llm._vertex_token()` uses google-auth Application Default Credentials, so any ADC
source works (SA key, `gcloud auth application-default login`, workload identity).

## 2. Brain provider

```bash
BRAIN_PROVIDER=vertex
VERTEX_PROJECT=868562221752
VERTEX_LOCATION=global          # required for gemini-3.5-flash; region ok for 2.5
VERTEX_MODEL=gemini-3.5-flash   # or gemini-2.5-flash
```

`google-auth` is in `requirements.txt` but lazy-imported — the rest of the app and
the whole test suite never need it. Tool-calling on Vertex is **not** wired (the
live tool path stays on Cerebras); `complete()` / `stream_complete()` (text) work.

Recommended split: **voice = Gemini** (see below), **brain/organization = Claude**
(reasons best on action extraction). Claude via *Vertex Model Garden* is a partner
model and is **not** covered by the GCP Free Trial — use Claude on its own Anthropic
API.

## 3. Realtime voice (Gemini Live) — spike only

Not wired into the live meeting path yet. A standalone spike to feel it lives in
[`backend/spikes/vertex_live/`](../../backend/spikes/vertex_live/). Key facts
(cost 6 probes to find):

- Bidi runs **only** on the `global` host — `wss://aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent`. The region-prefixed host 1008s with "was not found".
- Model: `gemini-live-2.5-flash`. Output audio is PCM **24 kHz** 16-bit mono; input is PCM **16 kHz**.
- Setup: `{"setup":{"model": "...gemini-live-2.5-flash", "generationConfig":{"responseModalities":["AUDIO"],"speechConfig":{"languageCode":"it-IT"}}}}`.
