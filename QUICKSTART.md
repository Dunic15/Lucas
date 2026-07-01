# Quickstart — the "insert one API key and it works" path

## 0. Requirements
- Python 3.9+ (`python3 --version`)
- ~2 minutes

## 1. Install
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 2. Run it (already works, for free)
```bash
uvicorn backend.app.main:app --port 8000
```
Open **http://127.0.0.1:8000**. Ask Lucas a question or click **Load sample →
Analyze meeting**. This runs offline in free "stub" mode — no keys, no cost.

## 3. Insert your key → real Claude answers
Open `.env` and paste your Anthropic key:
```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```
Get one at https://console.anthropic.com/ . Restart the server. Done — the app
detects the key and upgrades from stub to Claude automatically. **That is the
entire setup.**

## Prefer 100% local & free (no Anthropic bill)?
Install [Ollama](https://ollama.com/), then:
```bash
ollama run llama3.2         # first run downloads the model
# in .env:
BRAIN_PROVIDER=ollama
```

## Command line (no browser)
```bash
python backend/scripts/ask.py "What approvals are needed before provisioning?"
python backend/scripts/simulate.py        # meeting transcript -> action list
```

## What about the talking face in a real Zoom call?
That's the optional live path and needs extra vendor keys (Recall.ai, Anam,
ElevenLabs). See [docs/DEMO.md](docs/DEMO.md) and [docs/FREE_TIER.md](docs/FREE_TIER.md).
The demo console above never touches those vendors.

## Trouble?
| Symptom | Fix |
|---|---|
| `pip install` fails on numpy | You're on old Python — the pins allow 3.9; run `pip install -U pip` first |
| Page says "backend unreachable" | The server isn't running / wrong port — re-run step 2 |
| Answers look "extractive"/quoted | You're in free stub mode — add `ANTHROPIC_API_KEY` for real reasoning |
| Changed a knowledge doc, no effect | Restart the server (it re-indexes on boot), or run `python backend/scripts/ingest.py` |
