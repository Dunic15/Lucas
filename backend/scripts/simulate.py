"""Run the post-meeting brain over a transcript file (offline meeting simulator).

Usage:
    python backend/scripts/simulate.py                       # uses Lucas's sample
    python backend/scripts/simulate.py path/to/transcript.txt
    python backend/scripts/simulate.py --avatar lucas meeting.txt

Prints the summary, gap checklist, and draft follow-up email — the same
artifact POST /sessions/{id}/end returns after a real meeting. Works offline
with BRAIN_PROVIDER=stub; use anthropic/ollama for a real summary.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars  # noqa: E402
from app.brain import post_meeting  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    avatar_id = "lucas"
    if args and args[0] == "--avatar":
        avatar_id, args = args[1], args[2:]

    avatar = avatars.load(avatar_id)
    if args:
        transcript = Path(args[0]).read_text()
    else:
        sample = avatar.dir / "sample_meeting.txt"
        if not sample.exists():
            print(f"No transcript given and no sample at {sample}")
            return
        transcript = sample.read_text()
        print(f"(using sample meeting: {sample})")

    artifact = post_meeting(avatar, transcript)
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
