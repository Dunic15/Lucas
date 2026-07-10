# Laura — Callable AI Process Avatar

> We turn company processes into real-time AI avatars that join meetings and guide teams live.

**Laura** is a live **AI process agent** you can *call into* a Zoom / Meet /
Teams meeting — not an avatar toy, not a notetaker. She makes meetings **ready
before they start, complete before they end, and actionable after they finish**:
she listens, answers questions grounded + cited from your process docs, silently
tracks the call against your process, and speaks up **once** if a critical step
is missing as the call wraps up.

## What Laura does

| Phase | In the meeting | Output |
|---|---|---|
| **Before — ready** | tracks whether the meeting/decision has what it needs to run (objective, owners, access, decisions to make) | a readiness read the moment it matters |
| **During — complete** | silently tracks required process steps, decisions, owners, risks; answers grounded questions live; flags a missing **critical** step *once* at wrap-up | in-meeting answers + one intervention |
| **After — actionable** | delivers the artifact: summary, decisions, actions, **missing steps**, a **readiness score (0–100)**, draft follow-up email | post-meeting execution |

The differentiators, in order: **process tracking → missing-step prevention →
readiness score → follow-up execution.** The avatar face is just the mouth (modes
are [below](#avatar-modes)).

## Why not just a notetaker?

Notetakers (Otter, Fireflies, Granola, the native Zoom/Meet AI) tell you *after*
the meeting what was said. Laura is **in** the meeting: she knows what the process
requires *while the decision is being made*, warns before a gap becomes a skipped
approval, and her artifact is a readiness assessment against your actual process —
not minutes.

## Meeting intelligence (MeetingState)

Every transcript line silently updates a per-meeting state
(`backend/app/meeting_state.py` — pure regex, zero model calls, zero latency
added): meeting type, stage, required/completed/missing steps (from
`avatars/laura/process_templates/`), decisions, owners. It powers:

- **One deterministic closing intervention** — if a critical step (e.g. security
  approval, DPA) is still missing when the meeting starts wrapping up, Laura says
  so, once.
- **The readiness score** — `readiness_score` (0–100) in the post-meeting
  artifact, computed from completed vs. required steps.

**Process templates** live in `avatars/<id>/process_templates/*.yaml` (id, name,
`required_steps`, `critical_gaps`) — add a template by dropping in a file, no code.
Laura ships four: `customer_onboarding`, `implementation_access` (can the team
actually start — access, technical owner, environment), `decision_quality` (did we
truly decide — options, decision, owner, next step), and `meeting_readiness`
(objective, agenda, right people, pre-read, decisions to make).

The artifact returned by `POST /sessions/{id}/end`:
`summary, decisions, actions (+ checklist alias), risks, missing_steps,
readiness_score, meeting_type, follow_up_email, transcript`.

## Quickstart — 60-second demo, zero keys

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # leave as-is to run free
uvicorn backend.app.main:app --port 8000
```

Open **http://127.0.0.1:8000** → ask Laura a question, or load the sample
meeting to get the full artifact. The demo runs the real brain + retrieval
offline (`stub` brain + `hash` embeddings) — **no API keys, ever**.

Want real model answers? Set `BRAIN_PROVIDER=anthropic` plus `ANTHROPIC_API_KEY`
in `.env` and restart — that's the simplest one-vendor setup. **Production runs
Cerebras** (`BRAIN_PROVIDER=cerebras`, `gemma-4-31b`) on the live path for the
fastest first token (~0.17s) — latency is the product in a meeting. Both Cerebras
and Groq (`BRAIN_PROVIDER=groq`) are OpenAI-compatible fast providers sharing one
implementation; when the fast provider rate-limits (429), a circuit breaker routes
live answers to **Claude Haiku** automatically, so the avatar never goes silent.
The post-meeting summary uses Claude Sonnet (`BRAIN_PROVIDER_POST=anthropic`) for
quality.

```bash
# CLI, offline:
python backend/scripts/ask.py "What approvals are needed before provisioning?"
python backend/scripts/simulate.py       # transcript -> full artifact
```

## Live meeting flow

Needs a publicly reachable backend (Recall must hit the webhook + avatar page)
plus `RECALL_API_KEY` and a brain key. Current live backend:
`https://dhfgfe6yw6.eu-central-1.awsapprunner.com` (AWS App Runner,
eu-central-1; Render is suspended).

Four ways to send Laura into a meeting:

1. **`/join` page** — paste a meeting URL, click Send Laura.
2. **Calendar** — invite `laura.ai.122222@gmail.com` to the event.
3. **Gmail watcher** — add her via Google Meet's "Add people".
4. **API** — `curl -X POST .../sessions/start -d '{"meeting_url": "..."}'`

In the call, just talk to her ("Laura, can we provision ACME today?"). She
answers grounded in the docs, or stays silent when not addressed (in-stream SKIP
gate — `REQUIRE_WAKE_WORD=false` by default, so she doesn't need her name for
grounded questions).

```bash
# End the session — returns the artifact AND stops the per-minute meters:
curl -X POST .../sessions/<bot_id>/end
```

**Always end sessions.** Recall bills per minute (legacy Anam too), and on the
photoreal path the GPU box costs money while running — `gpu/stop.sh` /
`gpu/status.sh` are the manual controls; the auto-stop layers are the safety net.

## Avatar modes

The face is just the mouth — the brain lives in the backend. The face is a
swappable page rendered by the Recall.ai bot as its camera, selected with
`AVATAR_PAGE`:

| Mode | Page | Face | Face cost |
|---|---|---|---|
| `talk` — **production default** | `frontend/talk.html` | open-source 3D avatar (TalkingHead WebGL, vendored `laura.glb`) + `/tts` voice | **$0/min** |
| `photoreal` — Stage 2 | `frontend/photoreal.html` | GPU-streamed MuseTalk face (`gpu/server.py`; stub engine needs no GPU) | ~$0.017/min, only while the GPU box runs |
| `avatar` — legacy | `frontend/avatar.html` | Anam (paid vendor) | per-minute vendor billing |

Photoreal degrades gracefully: **GPU stream → static portrait → `/talk`** — the
meeting always has a face. The GPU box is never always-on: four independent
auto-stop layers (launch TTL, boot TTL, idle watchdog, meeting-bound start/stop)
are documented in [gpu/README.md](gpu/README.md).

## Environment variables (the ones that matter)

Everything lives in `backend/app/config.py` (env var = UPPER_CASE field name).

| Group | Vars |
|---|---|
| Brain | `BRAIN_PROVIDER` (`anthropic`(default)`\|cerebras\|groq\|ollama\|stub`), `ANTHROPIC_API_KEY`, `CEREBRAS_API_KEY` (prod live path), `GROQ_API_KEY` (alt fast path), `BRAIN_MODEL` (Sonnet, post-meeting), `BRAIN_MODEL_FAST`/`BRAIN_MODEL_COMPLEX` |
| Retrieval | `EMBEDDING_PROVIDER` (`hash\|local\|voyage`) |
| Meeting (ears) | `RECALL_API_KEY`, `RECALL_API_BASE` (use `https://eu-central-1.recall.ai`), `RECALL_TRANSCRIPTION_*` |
| Face | `AVATAR_PAGE` (`talk\|photoreal\|avatar`), `ANAM_API_KEY`/`ANAM_AVATAR_ID` (legacy only) |
| Voice | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` (unset → free edge-tts) |
| Photoreal GPU | `GPU_STREAM_URL`, `GPU_INSTANCE_ID`, `GPU_AWS_REGION`, `GPU_IDLE_STOP_MINUTES` |
| Behavior | `WAKE_WORDS`, `REQUIRE_WAKE_WORD`, `SPEAK_COOLDOWN_SECONDS`, `PROACTIVE_ENABLED` |
| Server | `PUBLIC_BASE_URL`, `HOST`, `PORT` |

## Architecture

```
Entry points: /join page · calendar invite · Gmail "Add people" · POST /sessions/start
   │
   ▼
Meeting (Zoom/Meet/Teams)
   │
   │  Recall.ai bot joins ──────────► renders AVATAR_PAGE as its camera
   │        │                            /talk (3D, default) · /photoreal (GPU) · /avatar (Anam)
   │        │ transcript.data webhook         ▲ speak: ws /ws/<id>  or  SSE + poll
   ▼        ▼                                 │
        backend (the BRAIN — FastAPI, App Runner)
        ├─ MeetingState: silent per-line process tracker (regex, no latency)
        ├─ when-to-speak: in-stream SKIP gate + cooldown (+ optional wake word)
        ├─ RAG over avatars/<id>/knowledge/*.md → cited answers
        ├─ Brain: Cerebras (live) / Sonnet (post) / Haiku (fallback) / stub  ├─ /tts: ElevenLabs | edge-tts
        ├─ gpu_runtime: start/stop the photoreal box around meetings
        └─ SQLite store: sessions, routing, artifacts
   │
   ▼
POST /sessions/{id}/end ─► artifact: transcript + summary + decisions + actions
                           + missing steps + readiness score + follow-up email
```

The moat-preserving choice: the **brain lives in our backend**; the avatar layer
is a commandable mouth. **Adding an avatar = adding a folder** (`avatars/<id>/` —
see [avatars/README.md](avatars/README.md)); no backend code.

## Deeper docs

- [docs/product/WEDGE.md](docs/product/WEDGE.md) — the wedge: what Laura is, who
  it's for, why it wins (single source of truth for positioning)
- [docs/ARCHITECTURE_CURRENT.md](docs/ARCHITECTURE_CURRENT.md) — how it actually
  works today: paths, models, providers, deploy (single source of truth for architecture)
- [.claude/CONTEXT.md](.claude/CONTEXT.md) — company brief, hard constraints (start here)
- [gpu/README.md](gpu/README.md) — photoreal GPU track: launch runbook + cost controls
- [avatars/README.md](avatars/README.md) — add an avatar in 3 steps
- [CODEX.md](CODEX.md) — parallel-work brief + the live-meeting integration contract
- [.claude/agents/README.md](.claude/agents/README.md) — agent team + operating model
- [docs/](docs/) — setup notes (demo, calendar, free tier), research, GTM/product/fundraise
- [docs/REPO_HYGIENE.md](docs/REPO_HYGIENE.md) — what's current vs. stale in this repo

## Security / cost / PII

- **No secrets in git** — `.env*` gitignored; only `.env.example` tracked.
- **Meters off when not meeting** — end sessions; never leave the GPU box running.
- **Transcripts are PII** — kept in memory + the artifact store only, **never
  logged** (mechanically enforced by `.claude/hooks/guard.py`).
- The offline demo touches no paid vendor and stores nothing.
