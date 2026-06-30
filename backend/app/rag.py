"""Minimal, transparent RAG over company process docs.

Indexing: chunk each markdown file under knowledge/, embed the chunks, store
vectors + metadata in a local JSON file.

Retrieval: embed the query, cosine-rank chunks, return the top-k with their
source filename + section so answers can cite them.

This is deliberately dependency-light (numpy + a JSON file) so the trust-
critical retrieval path is fully inspectable. For production scale, swap this
module for Qdrant / pgvector behind the same `retrieve()` signature.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict

import numpy as np

from .config import settings
from .embeddings import embed


@dataclass
class Chunk:
    text: str
    source: str  # filename, e.g. "onboarding_sop.md"
    section: str  # nearest preceding markdown heading


def _chunk_markdown(text: str, source: str) -> list[Chunk]:
    """Chunk by markdown heading, keeping the heading as section metadata."""
    chunks: list[Chunk] = []
    section = "(intro)"
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            chunks.append(Chunk(text=body, source=source, section=section))

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            buffer = []
            section = line.lstrip("#").strip()
        else:
            buffer.append(line)
    flush()
    return chunks


def build_index() -> int:
    """(Re)build the vector store from knowledge/*.md. Returns chunk count."""
    all_chunks: list[Chunk] = []
    for path in sorted(settings.knowledge_dir.glob("*.md")):
        all_chunks.extend(_chunk_markdown(path.read_text(), path.name))

    if not all_chunks:
        raise RuntimeError(f"No .md docs found in {settings.knowledge_dir}")

    vectors = embed([c.text for c in all_chunks], input_type="document")

    settings.vector_store_path.parent.mkdir(parents=True, exist_ok=True)
    settings.vector_store_path.write_text(
        json.dumps(
            {
                "model": settings.embedding_model,
                "chunks": [asdict(c) for c in all_chunks],
                "vectors": vectors,
            }
        )
    )
    return len(all_chunks)


_CACHE: dict | None = None


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        if not settings.vector_store_path.exists():
            raise RuntimeError(
                "Vector store missing — run `python backend/scripts/ingest.py` first."
            )
        raw = json.loads(settings.vector_store_path.read_text())
        raw["matrix"] = np.array(raw["vectors"], dtype=np.float32)
        _CACHE = raw
    return _CACHE


@dataclass
class Retrieved:
    text: str
    source: str
    section: str
    score: float


def retrieve(query: str, k: int = 4) -> list[Retrieved]:
    store = _load()
    qv = np.array(embed([query], input_type="query")[0], dtype=np.float32)

    matrix = store["matrix"]
    # Cosine similarity.
    denom = np.linalg.norm(matrix, axis=1) * np.linalg.norm(qv) + 1e-9
    scores = matrix @ qv / denom
    top = np.argsort(-scores)[:k]

    out: list[Retrieved] = []
    for i in top:
        c = store["chunks"][int(i)]
        out.append(
            Retrieved(
                text=c["text"],
                source=c["source"],
                section=c["section"],
                score=float(scores[int(i)]),
            )
        )
    return out
