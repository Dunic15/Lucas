"""Minimal, transparent RAG — per avatar.

Each avatar has its own knowledge folder and its own index, so avatars never
mix knowledge. Indexing chunks markdown by heading, embeds the chunks, and
stores vectors + metadata in `avatars/<id>/.index.json`.

Retrieval embeds the query, cosine-ranks chunks, and returns the top-k with
source filename + section so answers can cite them.

Dependency-light on purpose (numpy + JSON) so the trust-critical retrieval path
is fully inspectable. Swap for Qdrant / pgvector behind `retrieve()` for scale.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict

import numpy as np

from .avatars import Avatar
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


def _chunk_plain(text: str, source: str, size: int = 900) -> list[Chunk]:
    """Chunk headingless text (PDF/txt) into ~size-char passages on paragraph breaks."""
    chunks: list[Chunk] = []
    buf = ""
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) > size and buf:
            chunks.append(Chunk(text=buf.strip(), source=source, section=""))
            buf = ""
        buf += para + "\n\n"
    if buf.strip():
        chunks.append(Chunk(text=buf.strip(), source=source, section=""))
    return chunks


def _read_pdf(path: Path) -> str:
    """Extract text from a PDF so you can drop a thesis/policy PDF into knowledge/."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError(
            "Reading PDFs needs pypdf: pip install pypdf (or drop a .md/.txt)."
        ) from e
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def build_index(avatar: Avatar) -> int:
    """(Re)build one avatar's vector store from its knowledge/ docs (.md/.txt/.pdf)."""
    all_chunks: list[Chunk] = []
    for path in sorted(avatar.knowledge_dir.glob("*")):
        if path.suffix.lower() == ".md":
            all_chunks.extend(_chunk_markdown(path.read_text(), path.name))
        elif path.suffix.lower() == ".txt":
            all_chunks.extend(_chunk_plain(path.read_text(), path.name))
        elif path.suffix.lower() == ".pdf":
            all_chunks.extend(_chunk_plain(_read_pdf(path), path.name))

    if not all_chunks:
        raise RuntimeError(
            f"No .md/.txt/.pdf docs found in {avatar.knowledge_dir}"
        )

    vectors = embed([c.text for c in all_chunks], input_type="document")

    avatar.index_path.write_text(
        json.dumps(
            {
                "provider": settings.embedding_provider,
                "model": settings.embedding_model,
                "chunks": [asdict(c) for c in all_chunks],
                "vectors": vectors,
            }
        )
    )
    _CACHE.pop(avatar.id, None)  # invalidate
    return len(all_chunks)


# avatar.id -> loaded store
_CACHE: dict[str, dict] = {}


def ensure_index(avatar: Avatar) -> None:
    """Build the index if it's missing or was built with a different embedder.

    Lets the demo 'just work' with no manual ingest step. Rebuilding is free and
    instant with the default hash embedder; other providers rebuild on switch.
    """
    if avatar.index_path.exists():
        try:
            provider = json.loads(avatar.index_path.read_text()).get("provider")
        except (json.JSONDecodeError, OSError):
            provider = None
        if provider == settings.embedding_provider:
            return
    build_index(avatar)


def _load(avatar: Avatar) -> dict:
    if avatar.id not in _CACHE:
        ensure_index(avatar)
        raw = json.loads(avatar.index_path.read_text())
        raw["matrix"] = np.array(raw["vectors"], dtype=np.float32)
        _CACHE[avatar.id] = raw
    return _CACHE[avatar.id]


@dataclass
class Retrieved:
    text: str
    source: str
    section: str
    score: float


def retrieve(avatar: Avatar, query: str, k: int = 4) -> list[Retrieved]:
    store = _load(avatar)
    qv = np.array(embed([query], input_type="query")[0], dtype=np.float32)

    matrix = store["matrix"]
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
