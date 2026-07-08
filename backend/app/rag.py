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
from pathlib import Path

import numpy as np

from .avatars import Avatar
from .config import settings
from .embeddings import embed


INDEX_VERSION = 3
CHUNK_TARGET_CHARS = 760
CHUNK_OVERLAP_CHARS = 160
_WORD = re.compile(r"[a-z0-9][a-z0-9_-]+", re.I)
_STOPWORDS = {
    "about",
    "after",
    "again",
    "before",
    "being",
    "could",
    "did",
    "didn",
    "does",
    "doesn",
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
_ALIASES = {
    "api_base": {"base", "region", "recall_api_base"},
    "apprunner": {"app_runner", "aws", "backend"},
    "auth": {"authentication", "key", "401"},
    "avatar": {"face", "mouth", "anam", "renderer"},
    "bot": {"recall", "meeting", "join"},
    "cloudflare": {"cloudflared", "tunnel"},
    "cloudflared": {"cloudflare", "tunnel"},
    "cost": {"price", "pricing", "billing", "meter"},
    "doesnt": {"not", "fail", "failed"},
    "eleven": {"elevenlabs", "voice"},
    "elevenlabs": {"eleven", "voice"},
    "gpu": {"web_gpu", "webgpu"},
    "grok": {"groq", "llm", "model"},
    "groq": {"grok", "llm", "model"},
    "join": {"arrive", "coming", "meeting"},
    "latency": {"slow", "lag", "laggy", "speed"},
    "mouth": {"avatar", "speak", "voice"},
    "recall": {"bot", "meeting", "webhook"},
    "recallai": {"recall", "transcription"},
    "speak": {"talk", "talking", "voice", "mouth"},
    "stt": {"transcription", "transcript", "speech"},
    "talk": {"speak", "speaking", "voice"},
    "talking": {"speak", "speaking", "voice"},
    "transcription": {"stt", "transcript", "speech"},
    "web_gpu": {"webgpu", "gpu", "variant"},
    "webgpu": {"web_gpu", "gpu", "variant"},
    "webhook": {"callback", "recall"},
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


def _collect_chunks(paths: list[Path]) -> list[Chunk]:
    all_chunks: list[Chunk] = []
    for path in paths:
        if path.suffix.lower() == ".md":
            all_chunks.extend(_chunk_markdown(path.read_text(), path.name))
        elif path.suffix.lower() == ".txt":
            all_chunks.extend(_chunk_plain(path.read_text(), path.name))
        elif path.suffix.lower() == ".pdf":
            all_chunks.extend(_chunk_plain(_read_pdf(path), path.name))
    return all_chunks


def _write_index(index_path: Path, chunks: list[Chunk]) -> None:
    vectors = embed([c.text for c in chunks], input_type="document")
    index_path.write_text(
        json.dumps(
            {
                "provider": settings.embedding_provider,
                "model": settings.embedding_model,
                "version": INDEX_VERSION,
                "chunks": [asdict(c) for c in chunks],
                "vectors": vectors,
            }
        )
    )


def build_index(avatar: Avatar) -> int:
    """(Re)build one avatar's vector store from its knowledge/ docs (.md/.txt/.pdf)."""
    all_chunks = _collect_chunks(
        [p for d in avatar.knowledge_dirs for p in sorted(d.glob("*"))]
    )
    if not all_chunks:
        raise RuntimeError(
            f"No .md/.txt/.pdf docs found in {avatar.knowledge_dir}"
        )
    _write_index(avatar.index_path, all_chunks)
    _CACHE.pop(avatar.id, None)  # invalidate
    return len(all_chunks)


# avatar.id -> loaded store
_CACHE: dict[str, dict] = {}


def _index_is_current(index_path: Path) -> bool:
    """True when the on-disk index matches the configured embedder + format."""
    if not index_path.exists():
        return False
    try:
        raw = json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return (
        raw.get("provider") == settings.embedding_provider
        and raw.get("model") == settings.embedding_model
        and raw.get("version") == INDEX_VERSION
    )


def ensure_index(avatar: Avatar) -> None:
    """Build the index if it's missing or was built with a different embedder.

    Lets the demo 'just work' with no manual ingest step. Rebuilding is free and
    instant with the default hash embedder; other providers rebuild on switch.
    """
    if _index_is_current(avatar.index_path):
        return
    build_index(avatar)


# ─────────────── "about" docs: self-knowledge, separate index ───────────────
# Meta docs about the avatar ITSELF (architecture, playbook, runbook, costs)
# used to live in knowledge/ and polluted process retrieval — a real "what's
# missing before go-live?" question pulled Laura's own playbook. They now live
# in avatars/<id>/about/ with their own index, consulted only when someone asks
# about the avatar herself (brain._is_about_avatar). No about/ folder = no
# index = retrieve_about returns [] — the feature is fully optional per avatar.
_ABOUT_CACHE: dict[str, dict] = {}


def _about_paths(avatar: Avatar) -> list[Path]:
    if not avatar.about_dir.exists():
        return []
    return [
        p
        for p in sorted(avatar.about_dir.glob("*"))
        if p.suffix.lower() in (".md", ".txt", ".pdf")
    ]


def build_about_index(avatar: Avatar) -> int:
    chunks = _collect_chunks(_about_paths(avatar))
    if not chunks:
        return 0
    _write_index(avatar.about_index_path, chunks)
    _ABOUT_CACHE.pop(avatar.id, None)
    return len(chunks)


def ensure_about_index(avatar: Avatar) -> None:
    if not _about_paths(avatar):
        return
    if _index_is_current(avatar.about_index_path):
        return
    build_about_index(avatar)


def retrieve_about(avatar: Avatar, query: str, k: int = 4) -> list[Retrieved]:
    """Top-k from the avatar's about/ docs; [] when the avatar has none."""
    ensure_about_index(avatar)
    if not avatar.about_index_path.exists():
        return []
    if avatar.id not in _ABOUT_CACHE:
        raw = json.loads(avatar.about_index_path.read_text())
        raw["matrix"] = np.array(raw["vectors"], dtype=np.float32)
        _ABOUT_CACHE[avatar.id] = raw
    return _rank(_ABOUT_CACHE[avatar.id], query, k)


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
    terms: set[str] = set()
    for m in _WORD.finditer(text):
        raw = m.group(0).lower()
        if raw in _STOPWORDS or len(raw) <= 2:
            continue
        variants = {raw, raw.replace("-", "_")}
        if "_" in raw or "-" in raw:
            variants.update(p for p in re.split(r"[_-]+", raw) if len(p) > 2)
        for term in variants:
            if term in _STOPWORDS:
                continue
            terms.add(term)
            terms.update(_stem_terms(term))

    expanded = set(terms)
    for term in terms:
        expanded.update(_ALIASES.get(term, set()))
    return expanded


def _stem_terms(term: str) -> set[str]:
    stems: set[str] = set()
    for suffix in ("ing", "ers", "er", "ed", "es", "s"):
        if len(term) > len(suffix) + 3 and term.endswith(suffix):
            stems.add(term[: -len(suffix)])
    return stems


def _phrases(text: str) -> set[str]:
    tokens = [
        m.group(0).lower().replace("_", " ")
        for m in _WORD.finditer(text)
        if m.group(0).lower() not in _STOPWORDS and len(m.group(0)) > 2
    ]
    out: set[str] = set()
    for n in (2, 3, 4):
        out.update(" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1))
    return out


def _lexical_score(query: str, query_terms: set[str], chunk: dict) -> float:
    if not query_terms:
        return 0.0
    source_section = " ".join(
        str(chunk.get(key, "")) for key in ("source", "section")
    )
    text = str(chunk.get("text", ""))
    text_terms = _terms(text)
    section_terms = _terms(source_section)

    text_overlap = query_terms & text_terms
    section_overlap = query_terms & section_terms
    coverage = len(text_overlap | section_overlap) / len(query_terms)
    section_coverage = len(section_overlap) / len(query_terms)

    query_phrases = _phrases(query)
    phrase_overlap = query_phrases & _phrases(f"{source_section}\n{text}")
    phrase_score = min(1.0, len(phrase_overlap) / max(1, min(4, len(query_phrases))))

    # Keep the score interpretable: 0 means no lexical support, 1 means strong
    # term + phrase coverage, especially in the source/heading.
    return min(
        1.0,
        (0.62 * coverage)
        + (0.18 * section_coverage)
        + (0.16 * phrase_score)
        + min(0.04, len(text_overlap) * 0.01),
    )


def warm(avatar: Avatar) -> None:
    """Pre-load the index into cache and warm the embedder at startup.

    Without this the FIRST live question pays the one-off cost of lazy-loading
    the embedding model (fastembed ONNX) plus reading/parsing the index — easily
    1-3s tacked onto the first answer. A throwaway retrieve does both eagerly.
    """
    try:
        retrieve(avatar, "warmup", k=1)
    except Exception as e:  # never let warm-up crash boot
        print(f"[startup] warm-up failed for '{avatar.id}': {e}", flush=True)


def retrieve(avatar: Avatar, query: str, k: int = 4) -> list[Retrieved]:
    return _rank(_load(avatar), query, k)


def _rank(store: dict, query: str, k: int) -> list[Retrieved]:
    qv = np.array(embed([query], input_type="query")[0], dtype=np.float32)

    matrix = store["matrix"]
    denom = np.linalg.norm(matrix, axis=1) * np.linalg.norm(qv) + 1e-9
    vector_scores = np.maximum(matrix @ qv / denom, 0.0)
    query_terms = _terms(query)
    lexical_scores = np.array(
        [
            _lexical_score(query, query_terms, chunk)
            for chunk in store["chunks"]
        ],
        dtype=np.float32,
    )
    combined = np.array(
        [
            (0.72 * float(vector_score)) + (0.28 * float(lexical_score))
            for vector_score, lexical_score in zip(vector_scores, lexical_scores)
        ],
        dtype=np.float32,
    )
    combined += np.where(lexical_scores >= 0.55, 0.06, 0.0).astype(np.float32)
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
