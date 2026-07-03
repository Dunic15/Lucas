# Laura — Company Brief (shared context for all agents)

_Every agent reads this first. Keep it factual and current — one edit propagates to the whole team._

## One-line pitch
**Callable AI Process Avatars.** Laura is an AI process expert you can call *into* a
Zoom / Meet / Teams meeting. She listens, answers grounded + cited from company
process docs (RAG over markdown), and can flag missing steps. After the call she
drafts a summary + gap checklist + follow-up email.

## The pain / ICP hypothesis
"The one person who actually knows the process isn't in the meeting." Teams stall,
approvals get skipped, onboarding/access/security steps get missed. ICP hypothesis:
ops-heavy teams (People/IT/Security/RevOps) at 50–500-person companies where
process is written down but not in everyone's head. **Unvalidated — treat as a
hypothesis to test, not a fact.**

## Stage (be honest — pre-revenue)
Working offline demo + a **live AWS test backend**. Not yet in paid customer
meetings. Knowledge base is still **3 demo docs** (onboarding, access/security, an
AI-buffer thesis) — real usefulness is gated on ingesting real process docs.

## Architecture (the brain is ours; vendors are swappable)
- **Backend = the BRAIN** — FastAPI, on **AWS App Runner (eu-central-1, 1 vCPU / 2 GB)**.
- **Recall.ai = ears + camera** — joins the meeting, streams transcripts, renders our
  avatar page as the bot's camera (Output Media).
- **Face + voice = a swappable "mouth"** — currently **Anam** (per-minute, the big
  cost); **being replaced** by an open-source in-browser avatar (TalkingHead 3D +
  free TTS) rendered in the Recall page. `frontend/talk.html` is the WIP replacement.
- **Brain (pluggable):** `groq` (live default — **llama-3.3-70b-versatile**, ~0.4s
  first token, no spikes) | `anthropic` | `ollama` | `stub` (offline, free).
- **Embeddings (pluggable):** `hash` (offline) | `local` (fastembed) | `voyage`.
- **RAG:** markdown docs per avatar → chunked by heading → cited answers.
- **Store:** SQLite session state (ephemeral on App Runner — no persistent disk).
- **Entry points:** Gmail "Add people" watcher, calendar auto-join, /join page.
- **Answer style (current):** conversational — greets, light small talk, general
  help, grounds process facts in docs, cites naturally, stays silent only when not
  addressed. (`brain.py` ANSWER_STREAM_SYSTEM.)

## Vendor cost reality (per 30-min meeting, 1 avatar)
- Recall bot ~$0.25 (recording) + ~$0.075 (transcription).
- **Anam ~$3.30–3.60 — ~85–90% of the cost.** Removing it (open-source avatar) is
  THE cost lever → a 30-min call drops to ~$0.40–0.80.
- Groq (llama-3.3-70b): ~$0.03–0.15. AWS App Runner: small + ~$15/mo always-on baseline.

## The moat / design principle
The brain lives in **our backend**; the avatar vendor is only a commandable mouth.
**Adding an avatar = adding a folder** (`avatars/<id>/`), no backend code.
Defensibility is in **knowledge ingestion + answer accuracy + verticalization**,
NOT the meeting plumbing (which is commoditizing).

## HARD CONSTRAINTS — no agent may violate these
1. **Never break the live-meeting integration CONTRACT** (see CODEX.md): the
   `ws://<host>/ws/<conversation_id>` connection, the `{type:"speak", text}` message
   handling, the `speak()` echo, the pinned face-SDK embed, and the
   `recall_client` / `anam_client` function signatures
   (`create_persona`, `create_conversation`, `end_conversation`).
2. **The offline demo must always run with ZERO API keys** (stub brain + hash
   embeddings). Never make the demo require a key.
3. **No secrets in git.** `.env` is gitignored; only `.env.example` is tracked.
4. **Synthetic, audit-safe data only** in `avatars/*/knowledge` — no real PII or
   customer names.
5. **Per-minute avatar billing:** sessions MUST be ended to stop the Recall + Anam
   meter. Never leave a session open.
6. **Transcripts are PII** — kept in memory only, **never logged**.
7. **Latency is the product** on the live path — no blocking calls between
   transcript → first spoken token.
