"""Build the RAG index for avatars from their knowledge/*.md docs.

Usage:
    python backend/scripts/ingest.py            # index every avatar
    python backend/scripts/ingest.py sofia      # index just one avatar
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a plain script: add backend/ to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars  # noqa: E402
from app.rag import build_index  # noqa: E402


def main() -> None:
    targets = sys.argv[1:] or avatars.list_ids()
    if not targets:
        print("No avatars found under avatars/. Add one (see avatars/README.md).")
        return

    for avatar_id in targets:
        avatar = avatars.load(avatar_id)
        count = build_index(avatar)
        print(f"  {avatar.id:<12} indexed {count} chunks -> {avatar.index_path}")


if __name__ == "__main__":
    main()
