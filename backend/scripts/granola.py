"""Pull a finished meeting transcript from Granola and analyze it (post-meeting).

Usage:
    python backend/scripts/granola.py list                 # list recent notes
    python backend/scripts/granola.py analyze <note_id>     # transcript -> artifact
    python backend/scripts/granola.py analyze <note_id> --avatar laura

Needs GRANOLA_API_KEY in .env (Granola app → Settings → Connectors → API keys).
The analysis uses the same brain as everything else (free stub, or Claude/Ollama).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars, granola_client  # noqa: E402
from app.brain import post_meeting  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]
    if cmd == "list":
        for n in granola_client.list_notes():
            print(f"  {n['id']}  {n['created_at']:<26} {n['title']}")
        return

    if cmd == "analyze" and len(args) >= 2:
        note_id = args[1]
        avatar_id = "laura"
        if "--avatar" in args:
            avatar_id = args[args.index("--avatar") + 1]
        transcript = granola_client.get_transcript(note_id)
        if not transcript.strip():
            print(f"No transcript found for note {note_id}.")
            return
        artifact = post_meeting(avatars.load(avatar_id), transcript)
        print(json.dumps(artifact, indent=2))
        return

    print(__doc__)


if __name__ == "__main__":
    main()
