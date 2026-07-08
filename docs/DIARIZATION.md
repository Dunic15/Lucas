# Diarization strategy — when and how pyannote enters the stack

Laura already gets **platform diarization** from Recall: every transcript line
arrives tagged with the participant's real platform identity, and the live
roster comes from participant events. For separate-device participants this is
*better* than any audio-based diarizer — real names, zero inference.

Audio diarization (pyannote) matters for exactly one gap: **several people
sharing one microphone** — a meeting-room laptop joins as a single participant
("Sala Riunioni") and all its voices collapse into one speaker in the
transcript, the roster, and per-person tracking.

pyannote is best-in-class here: `pyannote/speaker-diarization-3.1` is the
strongest open diarizer, and pyannoteAI's hosted models top the same
benchmarks. Adoption is staged so we never pay infra before the gap is proven
to matter for real users:

## Stage 0 — validate offline (SHIPPED: `scripts/diarize_recording.py`)

Run diarization on recordings of real meetings with a shared-mic room.
Compare the speaker timeline against what the transcript claimed. Decision
gate: does per-person attribution actually improve enough to change the
artifact/answers? The script supports two backends with identical output:

- `--backend hosted` (default when `PYANNOTE_API_KEY` is set): pyannoteAI
  API, Precision-2 model — zero installs, spends the free 100 hours. Audio
  is uploaded to their temporary storage (~24h retention), so use consented
  recordings only.
- `--backend local`: open-source 3.1 on the laptop (torch ~2GB stays OUT of
  the deploy image; CPU is fine offline). Run both on the same recording to
  see whether the paid model's edge matters for our case.

> **Account status (2026-07-08):** pyannoteAI account created with **100 free
> hours** of diarization. That removes the cost gate on stages 0–1: validation
> and the first live pilots can run entirely on the free allowance. API key is
> NOT in the repo — goes to `.env` locally / SSM on App Runner when stage 1
> lands. Relevant hosted features (docs.pyannote.ai): batch `diarize` jobs +
> webhooks + media upload (stage 0/1 batch), a **realtime streaming API**
> (create-stream / stream-audio — makes stage 1 simpler than the DIY diart
> plan below), and **voiceprint + identify** jobs (enroll a speaker once,
> recognize them across meetings — upgrade path from "Sala Riunioni · voice 2"
> to a persistent named identity).

## Stage 1 — live, pay-per-use (when stage 0 says yes AND a prospect has
shared-mic rooms)

- Subscribe the bot to Recall's realtime **separate per-participant audio**
  stream (websocket) for shared-mic participants only.
- Send that audio to the **pyannoteAI API** (hosted, better models than OSS,
  no infra) and merge its labels as sub-speakers at the `resolve_speaker()`
  seam: `"Sala Riunioni · voice 2"`. Everything downstream (MeetingState
  per-person, roster block, artifact) picks the labels up unchanged.
- Runs PARALLEL to the hot path — labels refine the transcript a beat later;
  speak decisions never wait on it. Latency stays the product.

## Stage 2 — self-host free (when the photoreal GPU box lands)

The GPU instance being provisioned for the photoreal avatar track can run
open-source pyannote/diart streaming at near-zero marginal cost. Swap the
stage-1 API call for the local pipeline behind the same interface. This is
when "open source = free" becomes actually true — before that box exists,
an always-on GPU for a handful of meetings costs more than the API.

## Why not on the live path today

- App Runner CPU containers: streaming diarization would fight the latency
  budget on the exact instance that must answer in under a second.
- pyannote.audio is batch-first; real-time needs the diart wrapper + label
  stability handling — a real subsystem, only worth building against proven
  demand.
- The failure it fixes (shared-mic attribution) degrades gracefully today:
  the room is one "participant" with merged notes — wrong-ish, not broken.
