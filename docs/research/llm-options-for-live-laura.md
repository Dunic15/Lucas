# LLM options for live Laura (researched 2026-07-06)

Two different jobs, two different models. The **live path** answers out loud in
the meeting; latency IS the product (per `.claude/CONTEXT.md`). The
**post-meeting path** builds the artifact after the meeting ends; quality
matters, latency doesn't. Since PR #16 they can run on different providers
(`BRAIN_PROVIDER` for live, `BRAIN_PROVIDER_POST` for the artifact).

All model names below are either verified from the project config
(`backend/app/config.py`, `.env`) or from vendor docs/benchmarks on the
retrieval date. Prices change; re-verify before cost-sensitive decisions.

## Measured on THIS system (2026-07-06, real keys, laura index warm)

| Path | Model (as configured) | Measured |
|---|---|---|
| Live: question → first speakable sentence | groq `llama-3.3-70b-versatile` | **~1.0s** warm (2.5s cold incl. index warmup; prod warms at boot) |
| Live: perceived (stop talking → she speaks) | + Recall transcript finalization + TTS | **~2–3s** |
| Post-meeting artifact (sample transcript) | groq `llama-3.3-70b-versatile` | 2.4s |
| Post-meeting artifact (sample transcript) | anthropic `claude-sonnet-5` | ~18s, visibly richer output |

## Live-path candidates

| Option | Exact model id | Price (in/out per 1M) | Latency | Notes |
|---|---|---|---|---|
| **Groq (current)** | `llama-3.3-70b-versatile` | $0.59 / $0.79 | ~0.95s TTFT, 250–330 tok/s ([Groq](https://groq.com/pricing), [Artificial Analysis](https://artificialanalysis.ai/models/llama-3-3-instruct-70b/providers)) | What we measured at ~1.0s first sentence. Cheapest and fastest of the text options. Weakest JSON discipline of the three; the SKIP-sentinel + streaming design already tolerates that. |
| Anthropic | `claude-haiku-4-5` | $1 / $5 | first token typically well under 1s; slightly slower than Groq in our stack | Better grounding/instruction-following than llama-3.3; 200K context. The default `BRAIN_MODEL_FAST` when running all-Anthropic. |
| Anthropic (quality live) | `claude-sonnet-5` | $3 / $15 (intro $2/$10 through 2026-08-31) | adaptive thinking adds latency. NOT recommended for the speak path | Near-Opus quality; use post-meeting instead. |
| OpenAI Realtime (speech-to-speech) | `gpt-realtime` / `gpt-realtime-mini`; `gpt-realtime-2` announced May 2026 ([OpenAI](https://openai.com/index/introducing-gpt-realtime/)) | audio: $32 in / $64 out per 1M audio tokens (~$0.04+/min real-world) ([pricing](https://developers.openai.com/api/docs/pricing), [field data](https://hackernoon.com/openai-realtime-api-pricing-in-2026-real-world-data-from-4000-measured-sessions)) | best-in-class conversational latency + built-in VAD/barge-in | **Architecture mismatch for Laura**: it's speech-in/speech-out, which would bypass our Recall transcript → RAG grounding → own-TTS/lip-sync pipeline (the trust layer and the avatar). Would be a provider migration, explicitly out of scope. Listed for completeness. |

## Post-meeting candidates

| Option | Exact model id | Price (in/out per 1M) | Fit |
|---|---|---|---|
| **Anthropic (recommended, now configured)** | `claude-sonnet-5` | $3 / $15 (intro $2/$10 thru 2026-08-31) | Near-Opus quality on structured/agentic output; strong JSON reliability and groundedness; ~18s per artifact is invisible post-meeting. |
| Anthropic (ceiling) | `claude-opus-4-8` | $5 / $25 | Step up when artifact quality matters more than cost (long multi-topic meetings). |
| Groq (previous) | `llama-3.3-70b-versatile` | $0.59 / $0.79 | 2.4s and cheap, but noticeably thinner summaries and weaker gap detection. Fine fallback. |
| OpenAI text | current text flagships exist but exact ids/prices were not verified in this pass. **unverified, do not configure from this doc** | - | No advantage over Sonnet 5 for this JSON-artifact workload that would justify adding a fourth provider. |

## Recommendation (implemented)

- **Live**: keep **Groq `llama-3.3-70b-versatile`** (`BRAIN_PROVIDER=groq`,
  `BRAIN_MODEL_FAST=llama-3.3-70b-versatile`). It is the measured latency
  winner and the live path's SKIP/streaming design covers its weaknesses.
  If Groq reliability ever becomes an issue, `claude-haiku-4-5` is the
  drop-in fallback (one env var).
- **Post-meeting**: **`claude-sonnet-5`** via the provider split
  (`BRAIN_PROVIDER_POST=anthropic`, `BRAIN_MODEL=claude-sonnet-5`).
- **Do not** adopt OpenAI Realtime for the meeting path: it replaces the
  grounding pipeline rather than plugging into it.

Sources: [Groq pricing](https://groq.com/pricing) ·
[Artificial Analysis llama-3.3 benchmark](https://artificialanalysis.ai/models/llama-3-3-instruct-70b/providers) ·
[OpenAI gpt-realtime announcement](https://openai.com/index/introducing-gpt-realtime/) ·
[OpenAI API pricing](https://developers.openai.com/api/docs/pricing) ·
[Realtime cost field data](https://hackernoon.com/openai-realtime-api-pricing-in-2026-real-world-data-from-4000-measured-sessions) ·
Anthropic model ids/prices from the project's claude-api reference (cached 2026-06).
