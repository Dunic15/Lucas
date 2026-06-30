# Callable AI Process Avatars

> We turn company processes into real-time AI avatars that can join meetings and guide teams live.

**Sofia** is the first avatar: an AI Process Expert you can *call into* a Zoom /
Meet / Teams meeting. She listens, and when addressed by name she answers from
your company's process docs — grounded and cited. After the call she drafts a
summary, a gap checklist, and a follow-up email.

This repo is the **MVP** of that idea, built to the "fail cheap, in order"
plan: prove the pipeline, then grounded answers, then the hard part
(knowing when to speak).

---

## Architecture

```
Meeting (Zoom/Meet/Teams)
   │
   │  Recall.ai bot joins ──────────────► renders /avatar as its camera
   │        │                                     │
   │        │ transcript.data (webhook)           │ embeds Tavus conversation
   ▼        ▼                                     ▼  (the FACE)
        backend  ◄───────────────────────────  Tavus replica
        (the BRAIN)                             voiced by ElevenLabs (the VOICE)
        ├─ when-to-speak gate (wake word + cooldown + confidence)
        ├─ RAG over knowledge/*.md  (Voyage embeddings → cosine retrieval)
        ├─ Claude: grounded, cited answer
        └─ on "speak" ─► websocket ─► avatar page ─► Tavus echo ─► avatar talks
   │
   ▼
   POST /sessions/{id}/end ─► Claude: summary + gap checklist + follow-up email
```

**Separation of concerns (the moat-preserving choice):** the *brain* lives in
our backend, driven by Recall's transcript. Tavus is only a *mouth + face* we
command via `echo`. That keeps the avatar vendor swappable — if Tavus changes
pricing or you prefer Anam/HeyGen, only `tavus_client.py` + `avatar.html`
change.

## Project layout

```
Lucas/
├── avatars/                  ← THE EDITABLE PART (no code to add an avatar)
│   ├── README.md             ← "how to add an avatar in 3 steps"
│   └── sofia/
│       ├── avatar.yaml       ← name, wake words, persona, face, voice
│       └── knowledge/        ← markdown process docs Sofia answers from
├── backend/
│   ├── app/
│   │   ├── main.py           ← API routes + the meeting flow (wiring)
│   │   ├── avatars.py        ← loads avatars/<id>/ into an Avatar object
│   │   ├── decision.py       ← when-to-speak gate (wake word, confidence)
│   │   ├── brain.py          ← Claude: grounded answers + post-meeting
│   │   ├── rag.py            ← retrieval over an avatar's knowledge
│   │   ├── recall_client.py  ← Recall.ai (ears + camera)
│   │   ├── tavus_client.py   ← Tavus face + ElevenLabs voice
│   │   ├── embeddings.py     ← Voyage embeddings (swappable)
│   │   ├── store.py          ← in-memory session state
│   │   └── config.py         ← all env settings in one place
│   ├── scripts/ingest.py     ← build the RAG index
│   └── tests/                ← pure-logic tests (no keys needed)
├── frontend/avatar.html      ← the page Recall renders as the bot's camera
├── .env.example              ← copy to .env, add keys
└── requirements.txt
```

**Where do I change X?**

| I want to… | Edit |
|---|---|
| Add / change an avatar's behaviour | `avatars/<id>/avatar.yaml` |
| Add process knowledge | drop `.md` in `avatars/<id>/knowledge/`, re-run ingest |
| Add a whole new avatar | copy `avatars/sofia/` → see `avatars/README.md` |
| Tune when it speaks | `backend/app/decision.py` (or per-avatar yaml) |
| Change how answers are phrased | `ANSWER_SYSTEM` in `backend/app/brain.py` |
| Swap the avatar/voice vendor | `backend/app/tavus_client.py` + `frontend/avatar.html` |
| Add a key / setting | `.env` + `backend/app/config.py` |

| Layer | Tool | Where |
|---|---|---|
| Meeting entry + transcript (ears) | Recall.ai | `backend/app/recall_client.py` |
| Face | Tavus replica | `backend/app/tavus_client.py`, `frontend/avatar.html` |
| Voice | ElevenLabs | configured as Tavus TTS layer in `tavus_client.py` |
| Reasoning (brain) | Claude | `backend/app/brain.py` |
| Knowledge retrieval (RAG) | Voyage embeddings + local store | `backend/app/rag.py` |
| When-to-speak gate | — | `backend/app/decision.py` |
| Avatar definitions (editable) | YAML + markdown | `avatars/<id>/` |

---

## Build status (what's proven vs. scaffold)

This follows the staged plan. Honest state today:

| Stage | What | Status |
|---|---|---|
| 1–2 | Pipeline: a face in a meeting that can speak | **Code complete, needs live keys to verify** |
| 3 | Live transcript → backend | **Code complete** (`/webhooks/recall`) |
| 4 | RAG with citations | **Code complete + locally runnable** (needs `VOYAGE_API_KEY`) |
| 5 | Answer-when-called (speak only when named) | **Code complete** |
| 6 | Post-meeting summary + checklist + email | **Code complete** |
| 7 | Controlled proactive intervention | **Not built yet** — deliberate next step |

**Integration seams that need a live run to confirm** (marked in-code):
- the Tavus `echo` app-message schema and that Recall captures the embedded
  Daily iframe audio (`frontend/avatar.html → speak()`);
- exact Recall Create-Bot field shapes for your API version
  (`recall_client.py`, isolated in one body).

Nothing here has been run against live Recall/Tavus/ElevenLabs accounts yet —
it is a faithful implementation of the verified architecture, ready for keys.

---

## Run it

### 1. Install
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in keys
```

### 2. Build the knowledge index (RAG)
```bash
python backend/scripts/ingest.py          # indexes every avatar
```
This embeds each avatar's `knowledge/*.md` into `avatars/<id>/.index.json`.
Edit / add process docs and re-run to refresh.

### 3. Start the backend
```bash
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```
Health check: <http://127.0.0.1:8000/health>

### 4. Expose it (Recall must reach your webhook + avatar page)
```bash
ngrok http 8000
# put the https URL in .env as PUBLIC_BASE_URL, restart uvicorn
```

### 5. Call Sofia into a meeting
```bash
curl -X POST http://127.0.0.1:8000/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"meeting_url": "https://meet.google.com/your-test-call"}'
# -> returns bot_id
```
In the call, say: **"Sofia, what are we missing for this onboarding?"**
(Call a different avatar with `"avatar_id": "<id>"` — see `avatars/README.md`.)

### 6. End + get the artifact
```bash
curl -X POST http://127.0.0.1:8000/sessions/<bot_id>/end
# -> summary + gap checklist + draft follow-up email
```

---

## When-to-speak policy

The MVP is **conservative on purpose**: Sofia speaks *only* when called by a
wake word (`WAKE_WORDS`, default `sofia`), never within `SPEAK_COOLDOWN_SECONDS`
of her last line, and only if Claude's grounded confidence ≥ `MIN_CONFIDENCE`.
If the docs don't support an answer, she says so rather than guessing.

**Stage 7 (next):** one controlled proactive intervention — e.g. at meeting end,
if a required owner/approval is missing, Sofia says a single line. Gate it behind
high confidence; default to silence. This is the real differentiator and is only
truly testable in live pilots.

---

## Configuration

All via `.env` (see `.env.example`). Keys needed for a full live run: Anthropic,
Voyage, Recall.ai, Tavus (+ a replica id), ElevenLabs (+ a voice id), and a
public URL. The RAG layer (steps 4) runs with just Anthropic + Voyage.

## Security / cost notes

- **No secrets in git.** `.env` is gitignored; only `.env.example` is tracked.
- **Per-minute avatar billing.** `/sessions/{id}/end` ends both the Recall bot
  and the Tavus conversation to stop the meter — always end sessions.
- **PII.** Transcripts contain personal data; this MVP keeps them in memory only
  and never logs them. Don't add transcript logging without a retention policy.
