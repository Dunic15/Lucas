# Demo runbook

Two ways to show this off.

## Path A — Free / offline (no keys, ~2 min)

The value pitch without touching a single paid vendor.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn backend.app.main:app --port 8000
```

Open **http://127.0.0.1:8000** and walk through:

1. **Ask the avatar** — e.g. *"What approvals are required before IT provisions
   access?"* → a grounded answer with the source document cited and a confidence
   bar. Ask something not in the docs → the avatar declines instead of hallucinating.
2. **Post-meeting artifact** — click **Load sample → Analyze meeting**. You get a
   summary, a checklist with flagged gaps (missing approval / owner / deadline),
   and a draft follow-up email.

Talking points:
- The brain answers **only** from `avatars/lucas/knowledge/*.md`, and cites.
- Adding an avatar = adding a folder (`avatars/README.md`). No code.
- This same brain drives the live meeting avatar — the demo just skips the face.

Upgrade the answer quality live by pasting `ANTHROPIC_API_KEY` into `.env` and
restarting; the header flips from "free offline mode" to the Claude model.

### CLI variant (for a terminal-only demo)
```bash
python backend/scripts/ask.py "Who approves elevated access?"
python backend/scripts/simulate.py       # transcript -> artifact JSON
```

## Path B — Live meeting (trials/paid)

A real avatar face joins a real call.

Prerequisites in `.env`: `ANTHROPIC_API_KEY`, `RECALL_API_KEY`, the Anam face
keys, `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID`. See
[FREE_TIER.md](FREE_TIER.md).

```bash
# 1. Expose the server publicly (Recall must reach the webhook + avatar page)
ngrok http 8000
# put the https URL in .env as PUBLIC_BASE_URL, restart uvicorn

# 2. Start a test call (Google Meet is easiest) and send Lucas in
curl -X POST http://127.0.0.1:8000/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"meeting_url": "https://meet.google.com/your-test-call"}'

# 3. In the call, say the wake word:
#    "Lucas, what are we missing for this onboarding?"

# 4. End the session and collect the artifact (also stops billing)
curl -X POST http://127.0.0.1:8000/sessions/<bot_id>/end
```

Integration seams to confirm on a first live run are listed in the README build
status and marked in-code (the avatar echo message and Recall audio capture).
