# Laura — Company Brief (shared context for all agents)

_Every agent reads this first. Keep it factual and current — one edit propagates to the whole team._

## One-line pitch
**Callable AI Process Avatars.** Laura is an AI process expert you can call *into* a
Zoom / Meet / Teams meeting. She listens, answers grounded + cited from company
process docs (RAG over markdown), silently tracks the meeting against a process
checklist (**MeetingState**), and flags a missing critical step once as the call
wraps up. Afterwards she delivers a full artifact: transcript, summary, decisions,
actions, missing steps, **readiness score**, and a draft follow-up email.

## The pain / ICP hypothesis
"The one person who actually knows the process isn't in the meeting." Teams stall,
approvals get skipped, onboarding/access/security steps get missed. ICP hypothesis:
ops-heavy teams (People/IT/Security/RevOps) at 50–500-person companies where
process is written down but not in everyone's head. **Unvalidated — treat as a
hypothesis to test, not a fact.**

## Stage (be honest — pre-revenue)
Working offline demo + a **live AWS backend** (App Runner, eu-central-1;
Render is suspended, kept only as a fallback). Not yet in paid customer meetings.
Knowledge base is still **3 demo docs** — real usefulness is gated on ingesting
real process docs. The photoreal GPU track is fully built and validated end-to-end
on the stub engine; real-GPU launch is blocked only on an AWS quota approval.

## Architecture (the brain is ours; vendors are swappable)
- **Backend = the BRAIN** — FastAPI, on **AWS App Runner (eu-central-1, 1 vCPU / 2 GB)**.
- **Recall.ai = ears + camera** — joins the meeting, streams transcripts, renders our
  avatar page as the bot's camera (Output Media).
- **Face + voice = swappable pages**, selected by `AVATAR_PAGE`:
  - **`/talk` — the low-cost path, live in production.** Open-source in-browser 3D
    avatar (TalkingHead WebGL + vendored `frontend/laura.glb`) + `/tts`
    (ElevenLabs with word timings; free edge-tts fallback). Zero face-vendor cost.
  - **`/photoreal` — Stage 2.** GPU-streamed MuseTalk face (`gpu/server.py`; a stub
    engine streams the same protocol with no GPU, for dev/testing). Fallback chain:
    GPU stream → static portrait → `/talk`. **GPU cost controls** are built in: four
    independent auto-stop layers (launch TTL, boot TTL, idle watchdog, meeting-bound
    start/stop from the backend) — see `gpu/README.md`.
  - **`/avatar` — legacy Anam (paid per-minute).** No longer the core/default story;
    kept as a working fallback. (Note: the *code* default of `AVATAR_PAGE` is still
    `"avatar"`; production overrides it to `talk`.)
- **MeetingState = the intelligence layer** (`backend/app/meeting_state.py` +
  `avatars/<id>/process_templates/`): a regex-only silent tracker updated on every
  transcript line — required/completed/missing steps, decisions, owners, stage.
  Zero model calls, zero latency added to the live path. Powers ONE deterministic
  closing intervention (when a critical step is missing) and the artifact's
  readiness score.
- **Brain (pluggable):** `groq` (live default — **llama-3.3-70b-versatile**, ~0.4s
  first token, no spikes) | `anthropic` | `ollama` | `stub` (offline, free).
- **Embeddings (pluggable):** `hash` (offline) | `local` (fastembed) | `voyage`.
- **RAG:** markdown docs per avatar → chunked by heading → cited answers.
- **Store:** SQLite session state (ephemeral on App Runner — no persistent disk).
- **Entry points:** Gmail "Add people" watcher, calendar auto-join, /join page,
  `POST /sessions/start`.
- **Answer style (current):** conversational — greets, light small talk, general
  help, grounds process facts in docs, cites naturally, stays silent only when not
  addressed (in-stream SKIP gate in `brain.py` ANSWER_STREAM_SYSTEM).
- **Post-meeting artifact** (returned by `/sessions/{id}/end`, saved in the artifact
  store): `summary, decisions, actions (+ checklist alias), risks, missing_steps,
  readiness_score, meeting_type, follow_up_email, transcript`. The transcript is
  persisted **into the artifact only** — see hard constraint 6.

## Vendor cost reality (per 30-min meeting, 1 avatar)
- Recall bot ~$0.25 (recording) + ~$0.075 (transcription); Groq ~$0.03–0.15.
- **Anam is out of the default path** — `/talk` has no face-vendor cost, so a 30-min
  call is ~$0.40–0.80 total (was ~$3.30–3.60 when Anam was the face).
- `/photoreal` adds ~$1.01/hr (**~$0.017/min, only while the GPU box runs**); the
  four auto-stop layers exist so it can never idle-bill.
- AWS App Runner: small + ~$15/mo always-on baseline.

## The moat / design principle
The brain lives in **our backend**; the avatar layer is only a commandable mouth.
**Adding an avatar = adding a folder** (`avatars/<id>/`), no backend code.
Defensibility is in **knowledge ingestion + answer accuracy + process templates +
verticalization**, NOT the meeting plumbing (which is commoditizing).

## HARD CONSTRAINTS — no agent may violate these
1. **Never break the live-meeting integration CONTRACT** (see CODEX.md): the
   `ws://<host>/ws/<conversation_id>` channel **and** its App-Runner-safe twins
   (SSE `/avatar/stream/<id>` + poll `/avatar/messages/<id>`), the
   `{type:"speak", text}` message handling, the `speak()` echo entry point and
   the **pinned** face-SDK embed in `frontend/avatar.html`, and the
   `recall_client` / `anam_client` function signatures.
2. **The offline demo must always run with ZERO API keys** (stub brain + hash
   embeddings). Never make the demo require a key.
3. **No secrets in git.** `.env` (and every `.env.*` variant except
   `.env.example`) is gitignored.
4. **Synthetic, audit-safe data only** in `avatars/*/knowledge` — no real PII or
   customer names.
5. **Meters must be OFF when not in a meeting:** end sessions to stop the Recall
   (and legacy Anam) per-minute billing, and the photoreal GPU box must never be
   left running (`gpu/stop.sh`; the auto-stop layers are the safety net, not an
   excuse).
6. **Transcripts are PII** — kept in memory and the artifact store only, **never
   logged**. (`.claude/hooks/guard.py` enforces this mechanically.)
7. **Latency is the product** on the live path — no blocking calls between
   transcript → first spoken token.
