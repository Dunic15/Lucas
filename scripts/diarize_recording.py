#!/usr/bin/env python3
"""Offline speaker diarization for meeting recordings — hosted pyannoteAI or local OSS.

Purpose (stage 0 of docs/DIARIZATION.md): validate on REAL meetings whether
pyannote materially improves speaker attribution for the one case platform
diarization can't solve — several people sharing one mic/laptop
("Sala Riunioni" joins as a single participant). Run it on a recording, eyeball
the speaker timeline against what Recall's transcript claimed, and only then
decide whether live diarization is worth infra.

Two backends, same output, so results are directly comparable:

  hosted — pyannoteAI API (Precision-2, their best model). Zero installs
           (stdlib only) and it draws on the account's free 100 hours.
           Default whenever PYANNOTE_API_KEY is set.
  local  — open-source pyannote/speaker-diarization-3.1 on this machine.
           Needs `pip install pyannote.audio torch torchaudio` (~2GB) + HF_TOKEN.

This script is deliberately OUTSIDE the backend app:
  - none of these dependencies ship in the deploy image,
  - it never touches the live path,
  - the demo stays key-free.

Usage:
    export PYANNOTE_API_KEY=...          # from dashboard.pyannote.ai
    python3 scripts/diarize_recording.py meeting.wav
    python3 scripts/diarize_recording.py meeting.wav --num-speakers 4
    python3 scripts/diarize_recording.py meeting.wav --backend local
    python3 scripts/diarize_recording.py meeting.wav --json out.json

PII note: meeting recordings are PII. The local backend keeps everything on
this machine. The hosted backend uploads the audio to pyannoteAI's temporary
storage (removed after ~24h) — use it only on recordings whose participants
are OK with that. Either way the JSON output holds only speaker turns
(start/end/label), no audio, no text.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

PYANNOTE_API = "https://api.pyannote.ai/v1"


def _fail(msg: str) -> "NoReturn":  # noqa: F821
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ─────────────────────────── hosted backend (pyannoteAI) ───────────────────────────

def _api(api_key: str, method: str, url: str, body: dict | None = None,
         raw: bytes | None = None) -> dict:
    """Minimal stdlib HTTP helper — keeps the script dependency-free."""
    import urllib.error
    import urllib.request

    data = raw if raw is not None else (json.dumps(body).encode() if body else None)
    req = urllib.request.Request(url, data=data, method=method)
    if raw is None:
        req.add_header("Authorization", f"Bearer {api_key}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
    else:  # pre-signed upload URL: no auth header, plain bytes
        req.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        if e.code == 402:
            _fail("pyannoteAI says 402 (subscription required) — check the "
                  "account's free-hours balance at dashboard.pyannote.ai")
        _fail(f"pyannoteAI {method} {url} failed: HTTP {e.code} — {detail}")
    return json.loads(payload) if payload.strip() else {}


def diarize_hosted(audio_path: str, api_key: str, model: str,
                   num_speakers: int) -> list[dict]:
    # 1) reserve a media:// slot and upload the file to the pre-signed URL
    safe = re.sub(r"[^a-zA-Z0-9\-_.]", "_", os.path.basename(audio_path))
    media_url = f"media://laura-stage0/{safe}"
    print(f"[diarize] uploading {audio_path} → {media_url} …")
    slot = _api(api_key, "POST", f"{PYANNOTE_API}/media/input", {"url": media_url})
    with open(audio_path, "rb") as f:
        _api(api_key, "PUT", slot["url"], raw=f.read())

    # 2) submit the diarization job
    job_body: dict = {"url": media_url, "model": model}
    if num_speakers:
        job_body["numSpeakers"] = num_speakers
    job = _api(api_key, "POST", f"{PYANNOTE_API}/diarize", job_body)
    job_id = job["jobId"]
    print(f"[diarize] job {job_id} submitted (model={model}), polling …")

    # 3) poll until it settles (no webhook needed for an offline run)
    deadline = time.monotonic() + 30 * 60
    while time.monotonic() < deadline:
        state = _api(api_key, "GET", f"{PYANNOTE_API}/jobs/{job_id}")
        status = state.get("status", "")
        if status == "succeeded":
            out = state.get("output", {})
            return [
                {"start": round(seg["start"], 2), "end": round(seg["end"], 2),
                 "speaker": seg["speaker"]}
                for seg in out.get("diarization", [])
            ]
        if status in ("failed", "canceled"):
            _fail(f"job {status}: {state.get('output', {}).get('error') or state}")
        time.sleep(5)
    _fail(f"job {job_id} still not done after 30 min — check dashboard.pyannote.ai")


# ─────────────────────────── local backend (OSS pyannote) ──────────────────────────

def diarize_local(audio_path: str, hf_token: str, num_speakers: int) -> list[dict]:
    if not hf_token:
        _fail("HF_TOKEN not set — the pyannote 3.1 model is gated; accept its "
              "terms on huggingface.co and export HF_TOKEN=hf_...")
    try:
        from pyannote.audio import Pipeline  # heavy import, on purpose here
    except ImportError:
        _fail("pyannote.audio not installed — run: pip install pyannote.audio torch torchaudio")

    print("[diarize] loading pyannote/speaker-diarization-3.1 (first run downloads ~1GB)…")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", use_auth_token=hf_token
    )
    try:  # use a GPU when there is one; CPU works, just slower than realtime
        import torch

        if torch.cuda.is_available():
            pipeline.to(torch.device("cuda"))
            print("[diarize] using CUDA")
    except Exception:  # noqa: BLE001
        pass

    kwargs = {"num_speakers": num_speakers} if num_speakers else {}
    print(f"[diarize] processing {audio_path} …")
    annotation = pipeline(audio_path, **kwargs)
    return [
        {"start": round(seg.start, 2), "end": round(seg.end, 2), "speaker": label}
        for seg, _, label in annotation.itertracks(yield_label=True)
    ]


# ─────────────────────────────────────── main ──────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("audio", help="path to a local audio file (wav/mp3/mp4)")
    ap.add_argument("--backend", choices=["auto", "hosted", "local"], default="auto",
                    help="auto = hosted when PYANNOTE_API_KEY is set, else local")
    ap.add_argument("--model", default="precision-2",
                    choices=["precision-2", "community-1"],
                    help="hosted backend only: which pyannoteAI model to run")
    ap.add_argument("--num-speakers", type=int, default=0,
                    help="fix the speaker count when known (else auto)")
    ap.add_argument("--json", default="", help="also write speaker turns to this JSON file")
    ap.add_argument("--api-key", default=os.environ.get("PYANNOTE_API_KEY", ""),
                    help="pyannoteAI API key (default: $PYANNOTE_API_KEY)")
    ap.add_argument("--hf-token", default=os.environ.get("HF_TOKEN", ""),
                    help="Hugging Face token for the local backend (default: $HF_TOKEN)")
    args = ap.parse_args()

    if not os.path.exists(args.audio):
        _fail(f"no such file: {args.audio}")

    backend = args.backend
    if backend == "auto":
        backend = "hosted" if args.api_key else "local"
        print(f"[diarize] backend=auto → {backend}")
    if backend == "hosted":
        if not args.api_key:
            _fail("PYANNOTE_API_KEY not set — get one at dashboard.pyannote.ai, "
                  "or run with --backend local")
        turns = diarize_hosted(args.audio, args.api_key, args.model, args.num_speakers)
    else:
        turns = diarize_local(args.audio, args.hf_token, args.num_speakers)

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
            json.dump({"backend": backend,
                       "model": args.model if backend == "hosted" else "oss-3.1",
                       "speakers": speakers, "talk_seconds": total, "turns": turns},
                      f, indent=2)
        print(f"\n[diarize] wrote {args.json}")


if __name__ == "__main__":
    main()
