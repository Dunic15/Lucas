"""Build the RAG index for avatars from their knowledge docs.

Usage:
    python backend/scripts/ingest.py                 # index every avatar
    python backend/scripts/ingest.py laura           # index just one avatar
    python backend/scripts/ingest.py laura --check   # index + smoke-test retrieval
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a plain script: add backend/ to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars  # noqa: E402
from app.rag import build_index, retrieve  # noqa: E402


SMOKE_QUERIES = [
    "why is Laura not talking",
    "what is RECALL_API_BASE",
    "does web_gpu improve quality",
    "should we change Groq",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "avatars",
        nargs="*",
        help="Avatar id(s) to index. Defaults to every avatar under avatars/.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="After indexing, run a few retrieval smoke queries and print top hits.",
    )
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        help="Extra retrieval smoke query to run with --check. Can be repeated.",
    )
    return parser.parse_args()


def _knowledge_docs(avatar) -> list[Path]:
    return [
        p
        for p in sorted(avatar.knowledge_dir.glob("*"))
        if p.suffix.lower() in {".md", ".txt", ".pdf"}
    ]


def _run_checks(avatar, extra_queries: list[str]) -> None:
    queries = SMOKE_QUERIES + extra_queries
    for query in queries:
        hits = retrieve(avatar, query, k=2)
        rendered = "; ".join(
            f"{h.source} · {h.section or '(plain)'} ({h.score:.3f})"
            for h in hits
        )
        print(f"    check: {query!r} -> {rendered}")


def main() -> None:
    args = _parse_args()
    targets = args.avatars or avatars.list_ids()
    if not targets:
        print("No avatars found under avatars/. Add one (see avatars/README.md).")
        return

    for avatar_id in targets:
        avatar = avatars.load(avatar_id)
        docs = _knowledge_docs(avatar)
        if not docs:
            raise SystemExit(f"No .md/.txt/.pdf docs found in {avatar.knowledge_dir}")
        print(f"{avatar.id}: indexing {len(docs)} doc(s)")
        for doc in docs:
            print(f"    - {doc.name}")
        count = build_index(avatar)
        print(f"  {avatar.id:<12} indexed {count} chunks -> {avatar.index_path}")
        if args.check:
            _run_checks(avatar, args.query)


if __name__ == "__main__":
    main()
