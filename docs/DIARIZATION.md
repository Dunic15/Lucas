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

Run the open-source model on recordings of real meetings with a shared-mic
room. Compare its speaker timeline against what the transcript claimed.
Decision gate: does per-person attribution actually improve enough to change
the artifact/answers? Deps (torch ~2GB) stay OUT of the deploy image; the
script runs on a laptop, CPU is fine offline.

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
