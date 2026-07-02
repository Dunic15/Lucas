# Callable AI Process Avatars

> We turn company processes into real-time AI avatars that can join meetings and guide teams live.

**Laura** is the first avatar: an AI Process Expert you can *call into* a Zoom /
Meet / Teams meeting. Laura listens and, when addressed by name, answers from
your company's process docs — grounded and cited. After the call Laura drafts
a summary, a gap checklist, and a follow-up email.

---

## ⚡ 60-second demo (no keys needed)

You do **not** need any API keys to see it work. The demo console runs the real
brain + retrieval over the sample process docs, fully offline and free.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # leave it as-is to run free
uvicorn backend.app.main:app --port 8000
```

Open **http://127.0.0.1:8000** → ask Laura a question, or load the sample
meeting and get an action checklist + follow-up email.

### Want real Claude-quality answers? Insert one key.

Open `.env`, paste your key into `ANTHROPIC_API_KEY=` (get one at
[console.anthropic.com](https://console.anthropic.com/)), restart. **That's the
whole setup** — the app auto-detects the key and upgrades from the free stub to
Claude. Nothing else changes.

> The full **live-in-a-real-meeting** experience (a talking face in a Zoom call)
> additionally needs Recall.ai + Anam + ElevenLabs — see [Live meeting workflow](#live-meeting-workflow-optional).

---

## What runs with what

| You have… | Brain | Embeddings | You get |
|---|---|---|---|
| **nothing** (default) | free `stub` | free `hash` | working demo, extractive grounded answers |
| **1 key** (Anthropic) | Claude | free `hash` | real reasoning, real summaries ← recommended |
| Ollama installed | local `llama3.2` | free | real reasoning, 100% local & free |
| all vendor keys | Claude | Voyage | live talking avatar in real meetings |

Switch any layer in `.env`: `BRAIN_PROVIDER` (`stub|ollama|anthropic`) and
`EMBEDDING_PROVIDER` (`hash|local|voyage`). See [docs/FREE_TIER.md](docs/FREE_TIER.md).

---

## Architecture

```
Entry points
   ├─ Chrome extension inside Google Meet
   ├─ Manual /join page
   ├─ Calendar invite / Gmail Meet invite watcher
   └─ Direct API call: POST /sessions/start
   │
   │
   ▼
Meeting (Zoom/Meet/Teams)
   │
   │  Recall.ai bot joins ─────────────► renders /avatar as its camera
   │        │                                     │
   │        │ transcript.data (webhook)           │ embeds Anam conversation
   ▼        ▼                                     ▼  (the FACE)
        backend  ◄───────────────────────────  Anam replica
        (the BRAIN)                             voiced by ElevenLabs (the VOICE)
        ├─ when-to-speak gate (wake word + cooldown + confidence)
        ├─ RAG over knowledge/*.md  (pluggable embeddings → cosine retrieval)
        ├─ Brain (Claude / Ollama / stub): grounded, cited answer
        ├─ SQLite session store (bots, routing, artifacts, dedupe)
        └─ on "speak" ─► websocket ─► avatar page ─► Anam talks
   │
   ▼
   POST /sessions/{id}/end ─► Brain: summary + gap checklist + follow-up email
```

**Separation of concerns (the moat-preserving choice):** the *brain* lives in
our backend, driven by Recall's transcript. The avatar vendor (Anam) is only a
*mouth + face* we command via `echo`. That keeps it swappable — if you prefer
Tavus/HeyGen, only the face-client + `avatar.html` change. The same brain also
powers the offline **demo console**, which needs no meeting vendor at all.

## Project layout

```
Laura/
├── avatars/                  ← THE EDITABLE PART (no code to add an avatar)
│   ├── README.md             ← "how to add an avatar in 3 steps"
│   └── laura/
│       ├── avatar.yaml       ← name, wake words, persona, face, voice
│       ├── knowledge/        ← markdown process docs Laura answers from
│       └── sample_meeting.txt← demo transcript for the post-meeting artifact
├── backend/
│   ├── app/
│   │   ├── main.py           ← API routes: demo console + live meeting flow
│   │   ├── avatars.py        ← loads avatars/<id>/ into an Avatar object
│   │   ├── decision.py       ← when-to-speak gate (wake word, confidence)
│   │   ├── brain.py          ← grounded answers + post-meeting (+ free stub)
│   │   ├── llm.py            ← pluggable brain: anthropic | ollama | stub
│   │   ├── rag.py            ← retrieval over an avatar's knowledge
│   │   ├── embeddings.py     ← pluggable embeddings: hash | local | voyage
│   │   ├── recall_client.py  ← Recall.ai (ears + camera, live meetings)
│   │   ├── gmail_watcher.py  ← watches Gmail Meet invites for live auto-join
│   │   ├── granola_client.py ← Granola (finished transcripts, post-meeting)
│   │   ├── anam_client.py    ← the avatar FACE (Anam) + ElevenLabs voice
│   │   ├── store.py          ← SQLite session state + transient websockets
│   │   └── config.py         ← all env settings in one place
│   ├── data/                 ← local SQLite store in development
│   ├── scripts/
│   │   ├── ingest.py         ← build the RAG index
│   │   ├── ask.py            ← ask an avatar from the CLI (offline)
│   │   ├── simulate.py       ← run the post-meeting brain on a transcript
│   │   └── granola.py        ← pull a Granola transcript and analyze it
│   └── tests/                ← pure-logic tests (no keys needed)
├── frontend/
│   ├── demo.html             ← the offline demo console (served at /)
│   ├── live.html             ← local live avatar/test page
│   ├── join.html             ← manual "send Laura to a meeting" console
│   └── avatar.html           ← the page Recall renders as the bot's camera
├── extensions/
│   └── laura-meet/           ← Chrome extension: Send Laura from Google Meet
├── docs/                     ← setup, demo, calendar, and deployment notes
├── .claude/agents/           ← helper subagents (run/ingest/author/deploy)
├── render.yaml               ← Render deployment blueprint
├── .env.example              ← copy to .env; the demo runs with it unchanged
└── requirements.txt
```

**Where do I change X?**

| I want to… | Edit |
|---|---|
| Add / change an avatar's behaviour | `avatars/<id>/avatar.yaml` |
| Add process knowledge | drop `.md` in `avatars/<id>/knowledge/`, restart (auto re-indexes) |
| Add a whole new avatar | copy `avatars/laura/` → see `avatars/README.md` |
| Switch the brain (Claude/Ollama/free) | `BRAIN_PROVIDER` in `.env` |
| Switch embeddings | `EMBEDDING_PROVIDER` in `.env` |
| Tune when it speaks | `backend/app/decision.py` (or per-avatar yaml) |
| Change how answers are phrased | `ANSWER_SYSTEM` in `backend/app/brain.py` |
| Swap the avatar/voice vendor | `backend/app/anam_client.py` + `frontend/avatar.html` |
| Send Laura from inside Google Meet | `extensions/laura-meet/` |
| Change the manual join page | `frontend/join.html` |
| Add a key / setting | `.env` + `backend/app/config.py` |

| Layer | Tool | Where |
|---|---|---|
| Meeting entry controls | Chrome extension / `/join` / calendar-Gmail watcher | `extensions/laura-meet/`, `frontend/join.html`, `main.py`, `gmail_watcher.py` |
| Meeting entry + transcript (ears) | Recall.ai (live) / Granola (post-meeting) | `recall_client.py`, `granola_client.py` |
| Face | Anam replica | `backend/app/anam_client.py`, `frontend/avatar.html` |
| Voice | ElevenLabs | configured as the face's TTS layer |
| Reasoning (brain) | Claude / Ollama / stub | `backend/app/brain.py`, `llm.py` |
| Knowledge retrieval (RAG) | pluggable embeddings + local store | `backend/app/rag.py`, `embeddings.py` |
| When-to-speak gate | — | `backend/app/decision.py` |
| Persistence | SQLite + in-memory websockets | `backend/app/store.py` |
| Avatar definitions (editable) | YAML + markdown | `avatars/<id>/` |

---

## Try it from the command line

```bash
# Ask a question (offline; add ANTHROPIC_API_KEY to .env for Claude answers)
python backend/scripts/ask.py "What approvals are needed before provisioning?"

# Turn a transcript into a summary + gap checklist + follow-up email
python backend/scripts/simulate.py            # uses Laura's sample_meeting.txt

# Pull a REAL finished transcript from Granola and analyze it (needs GRANOLA_API_KEY)
python backend/scripts/granola.py list
python backend/scripts/granola.py analyze <note_id>
```

**Where do transcripts come from?** For the **live** in-call agent, Recall.ai
streams the transcript in real time (and renders the avatar). For the
**post-meeting** artifact you can instead pull a finished transcript from
[Granola](https://docs.granola.ai/) — no Recall/Anam needed. Granola is
post-meeting only: it can't join a call or render the avatar, so it can't power
the live agent.

---

## Live meeting workflow (optional)

Yes: after the live keys are set, you can send Laura into a real meeting and
call on the agent by saying `Laura`. The server must be running and reachable
from the public internet so Recall.ai can post transcripts to the webhook and
render the avatar page.

Required `.env` values for the live workflow:

```bash
ANTHROPIC_API_KEY=...
RECALL_API_KEY=...
ANAM_API_KEY=...
ANAM_AVATAR_ID=...
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
PUBLIC_BASE_URL=https://your-public-ngrok-or-deploy-url
WAKE_WORDS=laura
```

You can send Laura into a live meeting in four ways:

1. **From Google Meet itself:** install the Chrome extension in
   `extensions/laura-meet/`, join a Meet call, then click **Send Laura**.
2. **From the manual web page:** open `/join`, paste a meeting URL, then click
   **Send Laura**.
3. **From calendar/Gmail:** invite `laura.ai.122222@gmail.com` to a real Google
   Calendar event, or add her from Google Meet's **Add people** flow if the Gmail
   watcher is configured.
4. **From the API:** call `POST /sessions/start` with the meeting URL.

`RECALL_API_KEY` must be a Recall API key, not a `whsec_...` workspace/webhook
secret. You can check the local setup without exposing secrets:

```bash
curl http://127.0.0.1:8000/recall/status
curl http://127.0.0.1:8000/recall/status?check_auth=true
```

Start the workflow:

```bash
# 1. Start the backend
source .venv/bin/activate
uvicorn backend.app.main:app --port 8000

# 2. In another terminal, expose the server publicly
ngrok http 8000

# 3. Put the ngrok HTTPS URL in .env as PUBLIC_BASE_URL, then restart uvicorn

# 4. Call Laura into a meeting, or use /join / the Chrome extension
curl -X POST http://127.0.0.1:8000/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"meeting_url": "https://meet.google.com/your-test-call"}'

# 5. In the call, say: "Laura, what are we missing for this onboarding?"

# 6. End the session and get the summary/checklist/email artifact
curl -X POST http://127.0.0.1:8000/sessions/<bot_id>/end
```

The `/sessions/start` response includes `bot_id`. Use that exact value in the
`/sessions/<bot_id>/end` call so Recall and Anam stop billing for the session.

For extra setup notes, see [docs/FREE_TIER.md](docs/FREE_TIER.md) and the demo
runbook in [docs/DEMO.md](docs/DEMO.md).

---

## When-to-speak policy

The MVP is **conservative on purpose**: Laura speaks *only* when called by a
wake word (`WAKE_WORDS`, default `laura`), never within `SPEAK_COOLDOWN_SECONDS`
of its last line, and only if the brain's grounded confidence ≥ `MIN_CONFIDENCE`.
If the docs don't support an answer, Laura says so rather than guessing.

**Next (Stage 7):** one controlled proactive intervention — e.g. at meeting end,
if a required owner/approval is missing, Laura says a single line. Gated behind
high confidence; default to silence.

## Security / cost notes

- **No secrets in git.** `.env` is gitignored; only `.env.example` is tracked.
- **Per-minute avatar billing.** `/sessions/{id}/end` ends both the Recall bot
  and the avatar conversation to stop the meter — always end sessions.
- **PII.** Transcripts contain personal data; this MVP keeps them in memory only
  and never logs them. Don't add transcript logging without a retention policy.
- The **demo console** touches none of the paid vendors and stores nothing.
