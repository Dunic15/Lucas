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
import re
from dataclasses import dataclass, asdict

import numpy as np

from .avatars import Avatar
from .config import settings
from .embeddings import embed


INDEX_VERSION = 2
CHUNK_TARGET_CHARS = 900
CHUNK_OVERLAP_CHARS = 180
_WORD = re.compile(r"[a-z0-9][a-z0-9_-]+", re.I)
_STOPWORDS = {
    "about",
    "after",
    "again",
    "before",
    "being",
    "could",
    "does",
    "during",
    "from",
    "have",
    "into",
    "laura",
    "meeting",
    "need",
    "needed",
    "should",
    "that",
    "their",
    "there",
    "these",
    "this",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
}


@dataclass
class Chunk:
    text: str
    source: str  # filename, e.g. "onboarding_sop.md"
    section: str  # nearest preceding markdown heading


def _chunk_markdown(text: str, source: str) -> list[Chunk]:
    """Chunk by markdown heading, then split large sections with overlap."""
    chunks: list[Chunk] = []
    section = "(intro)"
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            for part in _split_text_windows(body):
                chunks.append(
                    Chunk(
                        text=_with_section_heading(section, part),
                        source=source,
                        section=section,
                    )
                )

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            buffer = []
            section = line.lstrip("#").strip()
        else:
            buffer.append(line)
    flush()
    return chunks


def _with_section_heading(section: str, body: str) -> str:
    if not section or section == "(intro)":
        return body
    return f"{section}\n{body}"


def _split_oversized_paragraph(text: str) -> list[str]:
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > CHUNK_TARGET_CHARS:
        cut = rest.rfind(" ", 0, CHUNK_TARGET_CHARS)
        if cut < CHUNK_TARGET_CHARS * 0.6:
            cut = CHUNK_TARGET_CHARS
        parts.append(rest[:cut].strip())
        rest = rest[max(0, cut - CHUNK_OVERLAP_CHARS):].strip()
    if rest:
        parts.append(rest)
    return parts


def _tail_overlap(paragraphs: list[str]) -> list[str]:
    kept: list[str] = []
    total = 0
    for para in reversed(paragraphs):
        next_total = total + len(para)
        if kept and next_total > CHUNK_OVERLAP_CHARS:
            break
        kept.insert(0, para)
        total = next_total
    return kept


def _split_text_windows(text: str) -> list[str]:
    """Split text into stable ~900 char windows with paragraph overlap."""
    chunks: list[str] = []
    current: list[str] = []

    def current_len() -> int:
        return sum(len(p) for p in current) + max(0, len(current) - 1) * 2

    def flush() -> None:
        nonlocal current
        body = "\n\n".join(current).strip()
        if body:
            chunks.append(body)
        current = _tail_overlap(current)

    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        if len(para) > CHUNK_TARGET_CHARS:
            if current:
                flush()
                current = []
            chunks.extend(_split_oversized_paragraph(para))
            continue
        if current and current_len() + len(para) + 2 > CHUNK_TARGET_CHARS:
            flush()
        current.append(para)

    if current:
        body = "\n\n".join(current).strip()
        if body and (not chunks or chunks[-1] != body):
            chunks.append(body)
    return chunks


def _chunk_plain(text: str, source: str) -> list[Chunk]:
    """Chunk headingless text (PDF/txt) into ~size-char passages on paragraph breaks."""
    return [
        Chunk(text=part, source=source, section="")
        for part in _split_text_windows(text)
    ]


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
                "version": INDEX_VERSION,
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
            raw = json.loads(avatar.index_path.read_text())
            provider = raw.get("provider")
            model = raw.get("model")
            version = raw.get("version")
        except (json.JSONDecodeError, OSError):
            provider = None
            model = None
            version = None
        if (
            provider == settings.embedding_provider
            and model == settings.embedding_model
            and version == INDEX_VERSION
        ):
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


def _terms(text: str) -> set[str]:
    return {
        m.group(0).lower()
        for m in _WORD.finditer(text)
        if m.group(0).lower() not in _STOPWORDS and len(m.group(0)) > 2
    }


def _lexical_boost(query_terms: set[str], chunk: dict) -> float:
    if not query_terms:
        return 0.0
    haystack = " ".join(
        str(chunk.get(key, "")) for key in ("source", "section", "text")
    )
    overlap = len(query_terms & _terms(haystack)) / len(query_terms)
    return min(0.18, overlap * 0.18)


def retrieve(avatar: Avatar, query: str, k: int = 4) -> list[Retrieved]:
    store = _load(avatar)
    qv = np.array(embed([query], input_type="query")[0], dtype=np.float32)

    matrix = store["matrix"]
    denom = np.linalg.norm(matrix, axis=1) * np.linalg.norm(qv) + 1e-9
    scores = matrix @ qv / denom
    query_terms = _terms(query)
    combined = np.array(
        [
            float(score) + _lexical_boost(query_terms, chunk)
            for score, chunk in zip(scores, store["chunks"])
        ],
        dtype=np.float32,
    )
    top = np.argsort(-combined)[:k]

    out: list[Retrieved] = []
    for i in top:
        c = store["chunks"][int(i)]
        out.append(
            Retrieved(
                text=c["text"],
                source=c["source"],
                section=c["section"],
                score=float(combined[int(i)]),
            )
        )
    return out
