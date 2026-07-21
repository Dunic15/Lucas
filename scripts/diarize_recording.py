#!/usr/bin/env python3
"""Offline speaker diarization for meeting recordings; pyannote.audio 3.1.

Purpose (stage 0 of docs/DIARIZATION.md): validate on REAL meetings whether
open-source pyannote materially improves speaker attribution for the one case
platform diarization can't solve; several people sharing one mic/laptop
("Sala Riunioni" joins as a single participant). Run it on a recording, eyeball
the speaker timeline against what Recall's transcript claimed, and only then
decide whether live diarization is worth infra.

This script is deliberately OUTSIDE the backend app:
  - none of these dependencies ship in the deploy image (torch is ~2GB),
  - it never touches the live path,
  - the demo stays key-free.

Setup (local machine, one-time):
    pip install pyannote.audio torch torchaudio
    # accept the gated model terms at hf.co/pyannote/speaker-diarization-3.1
    export HF_TOKEN=hf_...

Usage:
    python3 scripts/diarize_recording.py meeting.wav
    python3 scripts/diarize_recording.py meeting.wav --num-speakers 4
    python3 scripts/diarize_recording.py meeting.wav --json out.json

PII note: meeting recordings are PII. Everything stays on this machine; the
JSON output holds only speaker turns (start/end/label), no audio, no text.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _fail(msg: str) -> "NoReturn":  # noqa: F821
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("audio", help="path to a local audio file (wav/mp3/mp4)")
    ap.add_argument("--num-speakers", type=int, default=0,
                    help="fix the speaker count when known (else auto)")
    ap.add_argument("--json", default="", help="also write speaker turns to this JSON file")
    ap.add_argument("--hf-token", default=os.environ.get("HF_TOKEN", ""),
                    help="Hugging Face token (default: $HF_TOKEN)")
    args = ap.parse_args()

    if not os.path.exists(args.audio):
        _fail(f"no such file: {args.audio}")
    if not args.hf_token:
        _fail("HF_TOKEN not set — the pyannote 3.1 model is gated; accept its "
              "terms on huggingface.co and export HF_TOKEN=hf_...")

    try:
        from pyannote.audio import Pipeline  # heavy import, on purpose here
    except ImportError:
        _fail("pyannote.audio not installed — run: pip install pyannote.audio torch torchaudio")

    print("[diarize] loading pyannote/speaker-diarization-3.1 (first run downloads ~1GB)…")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", use_auth_token=args.hf_token
    )
    try:  # use a GPU when there is one; CPU works, just slower than realtime
        import torch

        if torch.cuda.is_available():
            pipeline.to(torch.device("cuda"))
            print("[diarize] using CUDA")
    except Exception:  # noqa: BLE001
        pass

    kwargs = {"num_speakers": args.num_speakers} if args.num_speakers else {}
    print(f"[diarize] processing {args.audio} …")
    annotation = pipeline(args.audio, **kwargs)

    turns = [
        {"start": round(seg.start, 2), "end": round(seg.end, 2), "speaker": label}
        for seg, _, label in annotation.itertracks(yield_label=True)
    ]
    speakers = sorted({t["speaker"] for t in turns})
    total = {s: round(sum(t["end"] - t["start"] for t in turns if t["speaker"] == s), 1)
             for s in speakers}

    print(f"\n[diarize] {len(speakers)} speakers detected, {len(turns)} turns")
    for s in speakers:
        print(f"  {s}: {total[s]}s of speech")
    print()
    for t in turns:
        print(f"  {t['start']:>8.2f} - {t['end']:>8.2f}  {t['speaker']}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"speakers": speakers, "talk_seconds": total, "turns": turns}, f, indent=2)
        print(f"\n[diarize] wrote {args.json}")


if __name__ == "__main__":
    main()
