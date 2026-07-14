# Design B spike — Gemini ears + Laura's gate + ElevenLabs voice

The combination you'd actually ship: **keep Laura's control layer and brand voice,
let Gemini upgrade the ears.** Proves that the realtime model integrates *with*
Laura's logic instead of replacing it.

```
browser mic (PCM16 16kHz) → Gemini Live [TEXT out]  ── ears + turn-taking
   on end-of-turn:
     user transcript → decision.detect_wake(laura)   ── the REAL gate
       addressed?  → reply → ElevenLabs (Laura's voice) → browser plays
       not addressed & wake required → she stays silent
```

Unlike the Design-A spike (`../vertex_live/`, full Gemini speech-to-speech with
Gemini's own voice), this one **imports `app.*`** to reuse the real gate
(`app.decision.detect_wake`) and the real voice config (`app.tts` settings), and
runs off the repo `.env`. Still **not** wired into the live meeting path.

## Run
```bash
cd backend
export PATH="$(brew --prefix)/share/google-cloud-sdk/bin:$PATH"   # for gcloud
/path/to/.venv/bin/python spikes/vertex_live_designb/server.py
# open http://localhost:8779
```
Needs: `gcloud auth login` (mints the Vertex token), an ElevenLabs key in `.env`
(`ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID`), and `websockets` in the interpreter.

- Toggle **«richiedi Laura»** to feel the gate: off = she answers everything; on =
  `decision.detect_wake` holds her back unless addressed by name (fuzzy match, so
  "Lara"/"Laur" still wake her — the real logic).
- Type in the box to test without a mic (e.g. `Laura, riassumi` vs `riassumi`).

Validated end-to-end 2026-07-14: gate release + gate hold + ElevenLabs audio.

## What this proves for the real integration
- **Intelligence layer is free**: everything downstream runs on the transcript,
  which Gemini provides (`inputAudioTranscription`).
- **Control layer plugs in**: `decision.py` sits between Gemini's turn and the
  voice — the same gates (wake / deference / leave) apply.
- **Brand voice kept**: ElevenLabs speaks, not Gemini.

Next: swap the reply source from Gemini's text to Laura's grounded brain (RAG +
Claude), and move the audio plumbing onto Recall Output Media. Separate PR.
See [`docs/infra/VERTEX-GEMINI-SETUP.md`](../../../docs/infra/VERTEX-GEMINI-SETUP.md).
