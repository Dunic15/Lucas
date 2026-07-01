"""Ask an avatar a question from the command line (no meeting needed).

Usage:
    python backend/scripts/ask.py "What approvals are needed before provisioning?"
    python backend/scripts/ask.py --avatar lucas "What are we missing for onboarding?"

Runs the same brain + RAG the live avatar uses. Works fully offline with
BRAIN_PROVIDER=stub; set BRAIN_PROVIDER=anthropic (+ key) for real answers.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars  # noqa: E402
from app.brain import answer_question  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    avatar_id = "lucas"
    if args and args[0] == "--avatar":
        avatar_id, args = args[1], args[2:]
    question = " ".join(args).strip()
    if not question:
        print(__doc__)
        return

    avatar = avatars.load(avatar_id)
    r = answer_question(avatar, question)
    print(f"\n{avatar.name}: {r['answer']}\n")
    print(f"  confidence: {r.get('confidence')}  sufficient: {r.get('sufficient_context')}")
    print(f"  cited: {', '.join(r.get('citations') or []) or 'none'}")
    for x in r.get("retrieved", []):
        print(f"    - {x['source']} · {x['section']} ({x['score']})")


if __name__ == "__main__":
    main()
