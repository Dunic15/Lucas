"""Build the RAG vector store from knowledge/*.md.

Run once (and after editing process docs):
    python backend/scripts/ingest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a plain script: add repo root to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import build_index  # noqa: E402
from app.config import settings  # noqa: E402


def main() -> None:
    print(f"Indexing markdown docs in: {settings.knowledge_dir}")
    count = build_index()
    print(f"Indexed {count} chunks -> {settings.vector_store_path}")


if __name__ == "__main__":
    main()
