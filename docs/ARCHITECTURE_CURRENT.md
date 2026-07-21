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
Live model in prod is **Cerebras `gemma-4-31b`** (fastest first-token, ~0.17s),
a first-class OpenAI-compatible provider (`BRAIN_PROVIDER=cerebras`); post-meeting
quality is **Claude Sonnet 5**; **Claude Haiku** is the complex-reasoning tier and
the circuit-breaker fallback if Cerebras rate-limits. **Groq** is a cheap swap-in
(identical wire format). Web search is **Claude's native `web_search`** tool. Runs
on AWS App Runner (eu-central-1), auto-deploys on merge to `main`. Demo runs key-free.

## Three request paths (do not confuse them)

| Path | Endpoint | Function | Model | Streaming? |
|---|---|---|---|---|
| **Live meeting** (spoken) | `POST /live/ask`, meeting webhook | `brain.answer_question_stream` | Cerebras (live) · Claude native search · Haiku for complex | yes (token deltas) |
| **Interactive** (act) | `POST /live/act` | `brain.answer_with_tools` | Cerebras/Groq tool-loop (OpenAI-compatible); Haiku fallback | no |
| **Post-meeting** (quality) | `POST /sessions/{id}/end`, `POST /demo/post_meeting` | `brain.post_meeting` | Sonnet (`BRAIN_PROVIDER_POST=anthropic`) | no |
| **Offline demo** | `POST /demo/ask` | stub brain + hash embeddings | none | no |

Latency is the product on the live path — that's why it's Cerebras + streaming,
and why `MeetingState` is pure regex (no model call per line).

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
  Sonnet/Opus get the dynamic-filtering tool automatically. **Never the fast provider.**
- **Clearly analytical** (compare/analyze/plan/…) → `("anthropic", brain_model_complex)`
  = Haiku direct — more reliable than the fast provider, dodges rate limits.
- **Everything else** → `(brain_provider, brain_model_fast)`. In prod `brain_provider=cerebras`,
  so this is Cerebras `gemma-4-31b`. Set `groq`/`anthropic` to swap the fast path.

## Providers & resilience (`backend/app/llm.py`)

- **Anthropic** — Claude. `_stream_anthropic` streams token-by-token. Complex-tier +
  post=Sonnet. Handles adaptive-thinking blocks (first content block may be thinking).
- **Cerebras / Groq** — OpenAI-compatible fast providers (one shared impl,
  `_compat_creds` picks the endpoint+key). Cerebras is prod's live path; both stream
  and support the function-calling tool loop (`complete_with_tools`).
- **Claude Haiku fallback** — if the primary live provider errors, `complete` /
  `stream_complete` fall back to `claude-haiku-4-5` so the avatar never goes silent.
- **Fast-provider circuit breaker** — Cerebras/Groq rate-limit (429) under load. On a
  failure the breaker **opens** for a cooldown (the 429's `Retry-After` if present, else
  30s, capped 300s); while open, live answers skip the fast provider and go to Haiku.
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
free `/tts`, **default, $0/min, what prod runs**), `photoreal` (GPU **Ditto**,
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
  `/laura/prod/*` (Anthropic, Cerebras, Recall, Google, ElevenLabs, Anam keys).
- **Prod live-path env:** `BRAIN_PROVIDER=cerebras`, `BRAIN_MODEL_FAST=gemma-4-31b`,
  `BRAIN_MODEL=claude-sonnet-5`, `BRAIN_PROVIDER_POST=anthropic`, `AVATAR_PAGE=talk`,
  `RECALL_API_BASE=https://eu-central-1.recall.ai`, `EMBEDDING_PROVIDER=local`.
  (Migration pending: prod still carries the legacy `BRAIN_PROVIDER=groq` +
  `GROQ_BASE=https://api.cerebras.ai/v1` tunnel with the Cerebras key under
  `GROQ_API_KEY`; flip to the first-class vars above after the code merges.)
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

## Addendum — canonical Action Control Plane + Company Brain (2026-07-17)

Two durable subsystems landed after the 2026-07-07 alignment pass (full specs
in [`product/UNIFIED-ACTION-CONTROL-PLANE.md`](product/UNIFIED-ACTION-CONTROL-PLANE.md)
and [`product/LAURA-COMPANY-BRAIN-SKILLS-BROWSER-ROADMAP.md`](product/LAURA-COMPANY-BRAIN-SKILLS-BROWSER-ROADMAP.md)):

| Concern | File |
|---|---|
| Canonical action vocabulary (schemas, risk, status aliases, param validation) | `backend/app/action_plane.py` |
| Durable action rows, execution claim, decision record | `backend/app/outbox_pg.py` (+ migration `0009_canonical_actions`) |
| Status weld point, claim/decision facades | `backend/app/ledger.py` |
| Approve/params/canonical-GET doors | `backend/app/org_api.py`, `backend/app/dashboard.py` |
| Company Brain: sources/documents/chunks/jobs DAL | `backend/app/knowledge/dal.py` (+ migration `0010_company_brain`) |
| Ingestion worker + index bridge (PG → per-org index files) | `backend/app/knowledge/ingest.py` |
| Raw file storage (S3 / local) | `backend/app/knowledge/storage.py` |
| Knowledge HTTP surface (`/org/knowledge/*` + dashboard twin) | `backend/app/knowledge/router.py` |

Load-bearing rules: every consequential write is one canonical action —
captured live, typed at finalize, `needs_details` when required params are
missing, approved behind a first-write-wins decision record plus an atomic
`approved → executing` claim (exactly one external write, any surface, any
instance). Org knowledge is durable in Postgres (FORCE RLS); the per-org
index files the live path ranks are derived data, rebuilt from Postgres at
ingest and boot. Both features are flag-gated (`NATIVE_EXECUTOR` pre-existing;
`COMPANY_BRAIN_ENABLED` default off) and the key-free demo is unchanged.
