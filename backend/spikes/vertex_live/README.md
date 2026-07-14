# Gemini Live voice spike

Talk to `gemini-live-2.5-flash` on Vertex AI and **hear** the realtime voice, to
decide whether it's worth wiring into Laura's live meeting path. **Nothing here is
imported by the app** — the live-meeting contract is untouched. Throwaway.

```bash
pip install websockets                 # spike-only dep (not in requirements.txt)
gcloud auth login                      # once — the proxy mints tokens from this
python backend/spikes/vertex_live/server.py
# open http://localhost:8777, click "Parla", talk
```

- Voice model: `gemini-live-2.5-flash` (only Live model on this project).
- **Global** websocket host only — the region-prefixed host 1008s "was not found".
- Mic in: PCM 16 kHz · voice out: PCM 24 kHz · automatic VAD for turn-taking · barge-in supported.
- Auth here is a short-lived `gcloud` bearer (dev). Prod would use a service
  account via `app.llm._vertex_token()`.

Env overrides: `VERTEX_PROJECT`, `VERTEX_LOCATION`, `VERTEX_LIVE_MODEL`,
`SPIKE_LANG` (default `it-IT`), `SPIKE_HTTP_PORT` (8777), `SPIKE_WS_PORT` (8778).

Config + the full brain-provider path: [`docs/infra/VERTEX-GEMINI-SETUP.md`](../../../docs/infra/VERTEX-GEMINI-SETUP.md).

## Next step (not done here)
Wiring into the live path means: Recall Output Media → this bidi stream instead of
Deepgram STT + ElevenLabs TTS, with the decision.py gates (when/what to say,
grounding) re-expressed as manual-response mode. That's a separate PR after the
feel is validated — recommended split stays **voice = Gemini, brain = Claude**.
