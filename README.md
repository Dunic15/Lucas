# Laura — Callable AI Process Avatars

> We turn company processes into real-time AI avatars that join meetings, act on your tools, and guide teams live.

**Laura** is a platform for **callable AI process avatars** — live agents you can
*call into* a Zoom / Meet / Teams meeting. Not a notetaker, not an avatar toy: an
avatar **listens**, answers grounded and cited from your process docs, silently
tracks the call against your process, speaks up **once** if a critical step is
missing as the call wraps up, and afterward drafts the artifact and (with your
approval) **executes the follow-up** on your tools.

The face is just the mouth — the intelligence lives in the backend. **Adding an
avatar is adding a folder** (`avatars/<id>/`), no code. Laura ships a
**zero-key offline demo** and a live path on **AWS App Runner**.

---

## ⚠️ Two live environments (since 2026-07-28)

v1 is **frozen** for customers while development continues. Two deployments,
same codebase, different branches:

| | **Customers** | **You (development)** |
|---|---|---|
| Link | `https://app.lauravatar.com/dashboard` | `https://48zmdue8kg.eu-central-1.awsapprunner.com/dashboard` |
| Branch | `frozen/v1` — never moves | `main` — deploys on every push |
| Service | `laura-backend` | `laura-backend-next` |

**Where to modify:** everything happens on `main`. Push → your environment
updates in ~7 min → customers see nothing until you deliberately flip them.

Three deploys are separate — this is the thing that confuses people:

| Change | How it ships |
|---|---|
| Backend, personas, dashboard, actions | `git push` (automatic) |
| Turn-taking / barge-in / follow-up window | `cd relay/cedric-voice-v2 && npx wrangler deploy` |
| Agent tools, knowledge, voice, LLM | `EL_AGENT_NAME_SUFFIX=" (v2)"` + `create_meeting_agent.py` |

**Never touch** `relay/cedric-voice/` (customers' bridge), and **never** run
`create_meeting_agent.py` without the suffix — it is idempotent by agent *name*
and will silently rewrite the agents your customers are talking to.

Full runbooks: **[`docs/NEXT-ENV.md`](docs/NEXT-ENV.md)** (develop + test) ·
**[`docs/FREEZE-V1.md`](docs/FREEZE-V1.md)** (freeze, hotfix, ship to customers).

---

## What an avatar does

| Phase | In the meeting | Output |
|---|---|---|
| **Before — ready** | tracks whether the meeting/decision has what it needs (objective, owners, access, decisions to make) | a readiness read the moment it matters |
| **During — complete** | silently tracks required process steps, decisions, owners, risks; answers grounded questions live; flags a missing **critical** step *once* at wrap-up | in-meeting answers + one intervention |
| **After — actionable** | delivers the artifact (summary, decisions, actions, **missing steps**, **readiness score 0–100**, draft follow-up email) — and executes approved actions on your connected tools | post-meeting execution |

Differentiators, in order: **process tracking → missing-step prevention →
readiness score → follow-up execution.** Positioned *against* notetakers
(Otter, Fireflies, Granola, native Zoom/Meet AI): they tell you *after* what was
said; Laura acts *during*, while the decision is being made.

---

## Beyond the meeting — the platform

The backend has grown from a meeting brain into a control platform for autonomous
avatars. Everything below is flag-gated and unaffected by the key-free demo:

- **Dashboard (Control Center)** — `frontend/dashboard.html`: pick an avatar, see
  a live 3D stage, manage connections, review the Company Brain, and approve
  actions. Backend at `app/api/dashboard.py`.
- **Connections** — three brokers, per-org:
  - **Google (native)** — Calendar + Gmail + Drive on your own OAuth (`app/api/oauth.py`), for the core, highest-frequency actions and the raw-token reads.
  - **Pipedream Connect** — any of 3,000+ apps (Asana, Jira, Notion, HubSpot, Linear, …) with managed OAuth + pre-built actions / a Connect Proxy (`app/pipedream_client.py`, `app/pipedream_executor.py`). Experimental, flag-gated.
  - **Cedric (Slack)** — Slack + the orchestrator glue lives in `app/cedric/`.
- **Company Brain** — durable org knowledge with cited retrieval; ingest from
  uploads or a Drive folder (`app/knowledge/`).
- **Action Control Plane** — capture → type → **approve** → execute, with an
  exactly-once execution claim, durable outbox, and receipts (`app/actions/`).
  Native execution runs on your Google directly; other apps route through
  Pipedream after approval.
- **Browser Operator** — a guarded, watched browser that can act on web apps
  behind the same approve-door (`app/browser/`).
- **Multi-tenancy & billing** — per-org isolation on a Postgres control plane
  (RLS), usage metering and Stripe billing (`app/persistence/`, `app/core/`).

---

## Meeting intelligence (MeetingState)

Every transcript line silently updates a per-meeting state
(`app/meeting/meeting_state.py` — pure regex, zero model calls, zero added
latency): meeting type, stage, required/completed/missing steps (from
`avatars/<id>/process_templates/`), decisions, owners. It powers **one
deterministic closing intervention** (if a critical step is still missing as the
call wraps up) and the **readiness score** in the artifact.

The artifact returned by `POST /sessions/{id}/end`:
`summary, decisions, actions (+ checklist alias), risks, missing_steps,
readiness_score, meeting_type, follow_up_email, transcript`. Ending the session
also **stops the per-minute meters**.

---

## Quickstart — 60-second demo, zero keys

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # leave as-is to run free
uvicorn backend.app.main:app --port 8000
```

Open **http://127.0.0.1:8000** → ask a question, or load the sample meeting for
the full artifact. The demo runs the real brain + retrieval **offline** (`stub`
brain + `hash` embeddings) — **no API keys, ever** (a hard constraint).

```bash
# CLI, offline:
python backend/scripts/ask.py "What approvals are needed before provisioning?"
python backend/scripts/simulate.py       # transcript -> full artifact
```

Want real model answers? Set `BRAIN_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` in
`.env` and restart — the simplest one-vendor setup.

---

## Live meeting flow

Needs a publicly reachable backend (Recall hits the webhook + avatar page), a
`RECALL_API_KEY`, and a brain key. Live backend:
`https://dhfgfe6yw6.eu-central-1.awsapprunner.com` (AWS App Runner, eu-central-1).

Four ways to send an avatar into a meeting:

1. **`/join` page** — paste a meeting URL, click Send.
2. **Calendar** — invite `laura.ai.122222@gmail.com` (or `+<avatar>@` for another avatar) to the event.
3. **Gmail watcher** — add her via Meet's "Add people".
4. **API** — `curl -X POST .../sessions/start -d '{"meeting_url":"…"}'`.

In the call, just talk to her ("Laura, can we provision ACME today?"). She
answers grounded in the docs, or stays silent when not addressed.

```bash
# End the session — returns the artifact AND stops the per-minute meters:
curl -X POST .../sessions/<bot_id>/end
```

**Always end sessions.** Recall bills per minute (legacy Anam too); on the
photoreal path the GPU box costs money while running (`gpu/stop.sh` /
`gpu/status.sh` are the manual controls; auto-stop layers are the safety net).

---

## Avatar modes

The face is a swappable page rendered by the Recall.ai bot as its camera,
selected with `AVATAR_PAGE`:

| Mode | Page | Face | Face cost |
|---|---|---|---|
| `talk` — **production default** | `frontend/talk.html` | open-source 3D avatar (TalkingHead WebGL, vendored `<id>.glb`) + `/tts` voice | **$0/min** |
| `photoreal` — Stage 2 | `frontend/photoreal.html` | GPU-streamed **Ditto** face (`gpu/server.py`; `AVATAR_ENGINE=stub` needs no GPU) | ~$0.02/min, only while the GPU box runs |
| `avatar` — legacy | `frontend/avatar.html` | Anam (paid vendor) | per-minute vendor billing |

Photoreal degrades gracefully: **GPU stream → static portrait → `/talk`** — the
meeting always has a face. The production photoreal engine is **Ditto** (Ant
Group; `gpu/Dockerfile.ditto`, `ghcr.io/dunic15/laura-ditto`), with `musetalk`
and `stub` behind the same `AVATAR_ENGINE` seam. Cost controls: four independent
auto-shutdown layers — see [gpu/README.md](gpu/README.md) and [gpu/DITTO-LIVE.md](gpu/DITTO-LIVE.md).

---

## Repository structure

```
Laura/
├── backend/                     FastAPI backend — the BRAIN (deployed to App Runner)
│   ├── app/
│   │   ├── main.py              app assembly + the live-meeting CONTRACT handlers (kept here on purpose)
│   │   ├── api/                 HTTP routers (dashboard, sessions, oauth, org_*, pipedream_api, health, …)
│   │   ├── brain/               reasoning: engine (answer_question / post_meeting), llm, rag, embeddings, tools
│   │   ├── meeting/             live-meeting logic: lifecycle, decision (when-to-speak), meeting_state, emotion
│   │   ├── actions/             Action Control Plane: executor, ledger, outbox(+pg), approval, scheduler
│   │   ├── knowledge/           Company Brain: ingest + durable knowledge (dal, storage, router)
│   │   ├── browser/             Browser Operator (B0): operator state machine, providers, policy
│   │   ├── cedric/              Cedric×Laura Slack-orchestrator glue (own README; seams tagged "# CEDRIC")
│   │   ├── integrations/        vendor clients: recall, google, asana, jira, gemini_ears, anam, tts, gmail_watcher
│   │   ├── persistence/         data layer: store (SQLite dev), control_plane (Postgres), billing
│   │   ├── core/                cross-cutting: config, auth, entitlements, security, crypto
│   │   ├── avatar/ · runtime/ · datafoundation/ · demo_mvp/    (resolution, gpu/runpod runtime, DF, demo MVP)
│   │   ├── pipedream_client.py · pipedream_executor.py         (Pipedream Connect: managed auth + Connect Proxy)
│   │   └── <legacy>.py          4-line compatibility shims re-exporting from the packages above (post-#329)
│   ├── alembic/                 Postgres control-plane migrations (0001 → 0014)
│   ├── scripts/                 dev/ops CLIs: ask.py, simulate.py, ingest.py, recall_check.py
│   └── tests/                   ~140 files / ~1,575 tests (key-free in CI)
├── frontend/                    face + product pages: talk/photoreal/avatar/live.html, dashboard.html,
│                                login/join/meetings/demo.html, privacy/terms.html, vendored <id>.glb models
├── avatars/                     one folder per avatar (laura, cedric, petra, sff, duccio) — see avatars/README.md
├── gpu/                         photoreal track: server.py, Ditto adapter + Dockerfile.ditto, cost-control scripts
├── relay/laura-ears/            Cloudflare Worker "ears" (App Runner refuses inbound WS → relays audio to Gemini)
├── demos/northstar/             isolated synthetic company for the end-to-end demo (used by app/demo_mvp)
├── extensions/laura-meet/       Chrome MV3 "Send Laura" overlay for Meet/Zoom/Teams
├── lovable/                     Lovable-generated marketing landing site (separate toolchain; not the app)
├── etc/litestream.yml           continuous S3 replication of the SQLite store (App Runner disk is ephemeral)
├── scripts/                     repo-level ops: start-with-litestream.sh, serve.sh, validate_rls.py, latency_probe.py
├── docs/                        architecture, product, infra, GTM, research, fundraise + docs/archive (history)
├── Dockerfile                   python:3.12-slim; ships backend/ + frontend/ + avatars/; runs uvicorn
├── requirements.txt · .env.example (20KB, annotated) · CLAUDE.md · CODEX.md · .claude/CONTEXT.md
```

> **After PR #329**, `backend/app/` is 14 domain packages; `main.py` was kept as
> app assembly + the live-meeting contract, and ~50 old flat modules became
> 4-line compatibility shims so `import app.store` (etc.) still resolves. Full
> map + what's current vs. stale: [docs/STRUCTURE.md](docs/STRUCTURE.md).

---

## The live-meeting contract (never break)

These handlers stay in `backend/app/main.py` with byte-identical behavior — every
running meeting depends on them:

- **Speak channel:** WS `/ws/<conversation_id>` + App-Runner-safe twins SSE
  `/avatar/stream/<id>` and poll `/avatar/messages/<id>` (a message is routed
  down exactly one path). Message shape `{"type":"speak","text":"…"}`.
- **Ears / audio:** `/webhooks/recall`, `/webhooks/recall-calendar`,
  `/realtime/recall-audio`.
- `recall_client` / `anam_client` signatures and the GPU stream protocol.

Full contract: [CODEX.md](CODEX.md). **Latency is the product on the live path.**

---

## Environment variables (the ones that matter)

All config is `backend/app/core/config.py` (env var = UPPER_CASE field name);
`.env.example` is the annotated template.

| Group | Vars |
|---|---|
| Brain | `BRAIN_PROVIDER` (`anthropic`·`cerebras`·`groq`·`vertex`·`ollama`·`stub`), `ANTHROPIC_API_KEY`, `CEREBRAS_API_KEY` (prod live path), `GROQ_API_KEY`, `BRAIN_MODEL`/`BRAIN_MODEL_FAST`, `BRAIN_PROVIDER_POST` |
| Retrieval | `EMBEDDING_PROVIDER` (`hash`·`local`·`voyage`) |
| Meeting (ears) | `RECALL_API_KEY`, `RECALL_API_BASE` (`https://eu-central-1.recall.ai`), `RECALL_TRANSCRIPTION_*`, `GEMINI_EARS_MODE` |
| Face | `AVATAR_PAGE` (`talk`·`photoreal`·`avatar`), `ANAM_API_KEY`/`ANAM_AVATAR_ID` (legacy) |
| Voice | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` (unset → free edge-tts) |
| Photoreal GPU | `GPU_STREAM_URL`, `AVATAR_ENGINE`, `RUNPOD_*`, `GPU_IDLE_STOP_MINUTES` |
| Platform (flag-gated) | `COMPANY_BRAIN_ENABLED`, `NATIVE_EXECUTOR`, `BROWSER_OPERATOR_ENABLED`, `PIPEDREAM_*`, `BILLING_ENABLED`, `LAURA_DATABASE_URL` |
| Server | `PUBLIC_BASE_URL`, `HOST`, `PORT` |

**Prod live path today:** Cerebras `gemma-4-31b` (~0.17s first token) with a
Claude Haiku circuit-breaker fallback on 429; the post-meeting summary uses Claude
Sonnet (`BRAIN_PROVIDER_POST=anthropic`). Cerebras and Groq are OpenAI-compatible
fast providers sharing one implementation.

---

## Deploy

- **AWS App Runner** service `laura-backend` (eu-central-1, 1 vCPU / 2 GB), live at
  `https://dhfgfe6yw6.eu-central-1.awsapprunner.com`. **Auto-deploys on merge to
  `main`** (~6–8 min); the root `Dockerfile` (python:3.12-slim, `uvicorn
  backend.app.main:app`) is the image. There is no `apprunner.yaml`.
- **Secrets** live in SSM `/laura/prod/*` (referenced by the service); **no
  secrets in git**. Runtime env vars live on the service — an `update-service`
  replaces the whole env map, so always describe → merge → update.
- **Durability:** the SQLite store is continuously replicated to S3 via Litestream
  (`etc/litestream.yml`, `scripts/start-with-litestream.sh`); the RAG index
  rebuilds on each deploy. The Postgres control plane (multi-tenancy/billing) is
  separate (Supabase), migrated via `backend/alembic/`.
- **Never deploy over a live meeting** — gate on `/health` `active_sessions == 0`.

---

## Deeper docs

- [docs/STRUCTURE.md](docs/STRUCTURE.md) — the repository map: every package/dir, what's current vs. stale
- [docs/ARCHITECTURE_CURRENT.md](docs/ARCHITECTURE_CURRENT.md) — how the engine works today (paths, models, providers, deploy)
- [docs/product/WEDGE.md](docs/product/WEDGE.md) — positioning: what Laura is, who it's for, why it wins
- [docs/product/roadmap.md](docs/product/roadmap.md) — the Now/Next/Later roadmap
- [.claude/CONTEXT.md](.claude/CONTEXT.md) — company brief + the 7 hard constraints (start here)
- [CODEX.md](CODEX.md) — the live-meeting integration contract
- [avatars/README.md](avatars/README.md) — add an avatar in 3 steps
- [gpu/README.md](gpu/README.md) · [gpu/DITTO-LIVE.md](gpu/DITTO-LIVE.md) — photoreal GPU track + cost controls
- [CLAUDE.md](CLAUDE.md) · [.claude/agents/README.md](.claude/agents/README.md) — working in the repo + the agent team

---

## Security / cost / PII

- **No secrets in git** — `.env*` gitignored; only `.env.example` tracked. Mechanically enforced by `.claude/hooks/guard.py`.
- **Meters off when not meeting** — end sessions; never leave the GPU box running.
- **Transcripts are PII** — kept in memory + the artifact store only, **never logged**.
- **Zero-key demo** — the offline demo touches no paid vendor and stores nothing.
