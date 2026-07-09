# Architecture — how Laura actually works today

> Single source of truth for **Laura's internal engine** — request paths, model
> routing, RAG, avatar face, deploy topology. If a comment, README line, or agent
> doc disagrees with this file about paths/models/providers, this file is right —
> fix the other one. For how Laura and Cedric fit together as **one system**
> (phases, hand-off config, standalone fallback) see [`ARCHITECTURE.md`](ARCHITECTURE.md).
> Pair with [`product/WEDGE.md`](product/WEDGE.md) (why it wins).
> Last aligned: repo-alignment pass, 2026-07-07.

## TL;DR

FastAPI backend (the **brain**) + a swappable avatar **face** rendered by a
Recall.ai bot as its meeting camera. The brain does silent process tracking
(`MeetingState`), grounded/cited answers (RAG), and the post-meeting artifact.
Live model is **Claude Haiku** (fast, not rate-limited); post-meeting quality is
**Claude Sonnet**; **Groq is an optional faster live provider** guarded by a
circuit breaker. Web search is **Claude's native `web_search`** tool. Runs on AWS
App Runner (eu-central-1), auto-deploys on merge to `main`. The demo runs key-free.

## Three request paths (do not confuse them)

| Path | Endpoint | Function | Model | Streaming? |
|---|---|---|---|---|
| **Live meeting** (spoken) | `POST /live/ask`, meeting webhook | `brain.answer_question_stream` | Haiku (live) · Claude native search · Haiku for complex | yes (token deltas) |
| **Interactive** (act) | `POST /live/act` | `brain.answer_with_tools` | Haiku; Groq tool-loop only if `BRAIN_PROVIDER=groq` | no |
| **Post-meeting** (quality) | `POST /sessions/{id}/end`, `POST /demo/post_meeting` | `brain.post_meeting` | Sonnet (`BRAIN_PROVIDER_POST=anthropic`) | no |
| **Offline demo** | `POST /demo/ask` | stub brain + hash embeddings | none | no |

Latency is the product on the live path — that's why it's Haiku + streaming, and
why `MeetingState` is pure regex (no model call per line).

## Live-meeting contract (never break)

- Recall bot renders `AVATAR_PAGE` as its camera and connects back for speech:
  **`ws /ws/{conversation_id}`** with **`{type:"speak", text}`** (SSE +
  `/avatar/messages/{id}` poll is the fallback transport).
- `recall_client` / `anam_client` signatures are load-bearing — keep them.
- End sessions (`POST /sessions/{bot_id}/end`) to stop the per-minute meter.
- Transcripts are PII: memory + artifact store only, **never logged** (enforced by
  `.claude/hooks/guard.py`).

## Model routing (`brain._live_route`)

For one live answer, `(provider, model)` is chosen by intent:

- **Fresh/current info** (weather, news, "latest…") → `("search", live_search_model)`
  → Claude's native `web_search` tool (`llm.web_search`). Haiku by default (fast);
  Sonnet/Opus get the dynamic-filtering tool automatically. **Never Groq.**
- **Clearly analytical** (compare/analyze/plan/…) → `("anthropic", brain_model_complex)`
  = Haiku direct — more reliable than the fast provider, dodges Groq limits.
- **Everything else** → `(brain_provider, brain_model_fast)`. In prod `brain_provider=anthropic`,
  so this is Haiku. If set to `groq`, it's Groq llama with the breaker below.

## Providers & resilience (`backend/app/llm.py`)

- **Anthropic** — Claude. `_stream_anthropic` streams token-by-token. Live=Haiku,
  post=Sonnet. Handles adaptive-thinking blocks (first content block may be thinking).
- **Groq** — optional fast provider (OpenAI-compatible). Streams; supports a
  function-calling tool loop (`complete_with_tools`).
- **Claude Haiku fallback** — if the primary live provider errors, `complete` /
  `stream_complete` fall back to `claude-haiku-4-5` so the avatar never goes silent.
- **Groq circuit breaker** — Groq's free tier 429s under load. On a Groq failure the
  breaker **opens** for a cooldown (the 429's `Retry-After` if present, else 30s,
  capped 300s); while open, live answers skip Groq entirely and go straight to Haiku.
  Self-heals when the window elapses. Process-local; reset between tests via
  `_reset_groq_breaker` (see `backend/tests/conftest.py`).
- **stub** / **ollama** — offline demo brain / local model.

## MeetingState (`backend/app/meeting_state.py`)

Every transcript line folds into a per-meeting state — **pure regex, zero model
calls, O(line)**, so the live path pays no added latency. It locks the meeting
*type* from the first hint line, tracks required→completed→missing steps, decisions,
owners, deadlines, risks, open questions, and computes `readiness_score` (0–100).

- **Templates:** `avatars/<id>/process_templates/*.yaml` (`id`, `name`,
  `required_steps`, `critical_gaps`). Shipped: `customer_onboarding`,
  `implementation_access`, `decision_quality`, `meeting_readiness`. Add one by
  dropping a file — type-hint + step-topic regexes in `meeting_state.py` cover the
  step vocabulary (unknown ids fall back to matching their own words).
- **Closing intervention:** at wrap-up, if a **critical** step never happened, Laura
  says one templated line (deterministic — no model, no retrieval). Gated by
  `PROACTIVE_ENABLED` + `PROACTIVE_MIN_CONFIDENCE`.

## When-to-speak (`backend/app/decision.py`)

Laura tracks silently **always**; the wake word only gates *speaking*. With
`REQUIRE_WAKE_WORD=false` (default) she answers any groundable question without her
name; the **in-stream SKIP sentinel** enforces grounding (empty/`SKIP` → stays
silent). `detect_wake` distinguishes a vocative ("Laura, …") from a third-person
mention ("as Laura said"). `detect_closing` drives the wrap-up; `detect_leave_command`
handles "Laura, you can leave". **`MIN_CONFIDENCE` / `passes_confidence` is legacy —
dead on the live streaming path** (superseded by the SKIP gate), kept for back-compat.

## Retrieval (RAG)

`rag.py` + `embeddings.py` over `avatars/<id>/knowledge/*.md`. Retrieved context is
injected **only when it actually matches** the question
(`score >= RAG_MIN_CONTEXT_SCORE`) — so general questions get the model's own
intelligence instead of doc-quoting. Embeddings: `hash` (offline default), `local`
(fastembed), or `voyage`.

## Avatar face (swappable, env-selected)

`AVATAR_PAGE` picks the page the bot renders: `talk` (open-source TalkingHead +
free `/tts`, **default, $0/min, what prod runs**), `photoreal` (GPU MuseTalk,
degrades to static portrait → `/talk`), `avatar` (Anam, paid, legacy). Same
`{type:"speak"}` ws contract for all three, so switching is env-only.

## Entry points (into a meeting)

`/join` page · calendar invite to `laura.ai.122222@gmail.com` (Recall Calendar V2)
· Gmail watcher ("Add people" in Meet) · `POST /sessions/start`. Webhooks:
`/webhooks/recall` (transcript/status), `/webhooks/recall-calendar` (scheduling).

## Storage (`backend/app/store.py`)

SQLite (`laura-store.sqlite3` on the persistent mount, else `backend/data/`) for
sessions, routing, and artifacts — survives process restarts. Live WebSocket objects
are in-memory. The App Runner container is otherwise ephemeral: the **RAG index
rebuilds fresh on each deploy**.

## Deploy topology

- **Backend:** AWS App Runner service `laura-backend`, **eu-central-1**,
  `https://dhfgfe6yw6.eu-central-1.awsapprunner.com`. Auto-deploys on merge to
  `main` (~6–7 min). Render is suspended.
- **Config:** runtime env vars on the service; **secrets in SSM** under
  `/laura/prod/*` (Anthropic, Groq, Recall, Google, ElevenLabs, Anam keys).
- **Prod live-path env:** `BRAIN_PROVIDER=anthropic`, `BRAIN_MODEL_FAST=claude-haiku-4-5`,
  `BRAIN_MODEL=claude-sonnet-5`, `BRAIN_PROVIDER_POST=anthropic`, `AVATAR_PAGE=talk`,
  `RECALL_API_BASE=https://eu-central-1.recall.ai`, `EMBEDDING_PROVIDER=local`.
- **Recall:** eu-central-1 workspace. **AWS writes are gated** — every `call_aws`
  needs explicit approval (`.claude/settings.json`).

## Key files

| Concern | File |
|---|---|
| HTTP/WS API, routing, webhooks | `backend/app/main.py` |
| Answer paths (stream / tools / post-meeting) | `backend/app/brain.py` |
| Providers, fallback, Groq breaker, web_search | `backend/app/llm.py` |
| Silent process tracker + templates | `backend/app/meeting_state.py` |
| Wake / closing / leave detection | `backend/app/decision.py` |
| Config (env → fields) | `backend/app/config.py` |
| RAG / embeddings | `backend/app/rag.py`, `backend/app/embeddings.py` |
| TTS router | `backend/app/tts.py` |
| Sessions/artifacts store | `backend/app/store.py` |
| Vendor clients | `backend/app/recall_client.py`, `anam_client.py` |
