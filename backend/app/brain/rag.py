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

from ..avatars import Avatar
from ..config import settings
from .embeddings import embed, provider_signature


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


_DOC_SUFFIXES = (".md", ".txt", ".pdf")


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


def _source_paths(dirs: list[Path]) -> list[Path]:
    """The doc files that actually feed an index, in stable order. Filtered to
    the indexable suffixes so a stray .DS_Store never triggers a rebuild."""
    return [
        p
        for d in dirs
        for p in sorted(d.glob("*"))
        if p.suffix.lower() in _DOC_SUFFIXES
    ]


def _sources_signature(paths: list[Path]) -> list[dict]:
    """Cheap freshness fingerprint of the source docs (no content read):
    path + size + mtime. Any edit, add, delete, or rename changes it — that's
    what lets _index_is_current spot a silently stale index."""
    sig = []
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue  # racing delete: the file is gone, so it's not a source
        sig.append({"path": str(p), "size": st.st_size, "mtime_ns": st.st_mtime_ns})
    return sig


def _write_index(index_path: Path, chunks: list[Chunk], source_paths: list[Path]) -> None:
    vectors = embed([c.text for c in chunks], input_type="document")
    index_path.write_text(
        json.dumps(
            {
                # The EFFECTIVE provider (hash when local fell back at boot) —
                # guarantees index/query vector agreement across restarts.
                "provider": provider_signature(),
                "model": settings.embedding_model,
                "version": INDEX_VERSION,
                "sources": _sources_signature(source_paths),
                "chunks": [asdict(c) for c in chunks],
                "vectors": vectors,
            }
        )
    )


def build_index(avatar: Avatar) -> int:
    """(Re)build one avatar's vector store from its knowledge/ docs (.md/.txt/.pdf)."""
    paths = _source_paths(avatar.knowledge_dirs)
    all_chunks = _collect_chunks(paths)
    if not all_chunks:
        raise RuntimeError(
            f"No .md/.txt/.pdf docs found in {avatar.knowledge_dir}"
        )
    _write_index(avatar.index_path, all_chunks, paths)
    _CACHE.pop(avatar.id, None)  # invalidate
    return len(all_chunks)


# avatar.id -> loaded store
_CACHE: dict[str, dict] = {}


def _index_is_current(index_path: Path, source_paths: list[Path]) -> bool:
    """True when the on-disk index matches the configured embedder + format AND
    the source docs it was built from. Editing/adding/removing a knowledge doc
    changes the signature, so the next ensure_index rebuilds instead of serving
    a silently stale index. (Indexes written before the signature existed lack
    the key and rebuild once.)"""
    if not index_path.exists():
        return False
    try:
        raw = json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return (
        raw.get("provider") == provider_signature()
        and raw.get("model") == settings.embedding_model
        and raw.get("version") == INDEX_VERSION
        and raw.get("sources") == _sources_signature(source_paths)
    )


def ensure_index(avatar: Avatar) -> None:
    """Build the index if it's missing, was built with a different embedder, or
    the knowledge docs changed since it was built.

    Lets the demo 'just work' with no manual ingest step. Rebuilding is free and
    instant with the default hash embedder; other providers rebuild on switch.
    Runs at boot, on ingest, and on first retrieval per process — an already-
    warmed process keeps serving its in-memory cache until then.
    """
    if _index_is_current(avatar.index_path, _source_paths(avatar.knowledge_dirs)):
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
    return _source_paths([avatar.about_dir])


def build_about_index(avatar: Avatar) -> int:
    paths = _about_paths(avatar)
    chunks = _collect_chunks(paths)
    if not chunks:
        return 0
    _write_index(avatar.about_index_path, chunks, paths)
    _ABOUT_CACHE.pop(avatar.id, None)
    return len(chunks)


def ensure_about_index(avatar: Avatar) -> None:
    paths = _about_paths(avatar)
    if not paths:
        return
    if _index_is_current(avatar.about_index_path, paths):
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


def retrieve(
    avatar: Avatar, query: str, k: int = 4, *, org_id: str = ""
) -> list[Retrieved]:
    """Top-k chunks for a query. With an ``org_id``, the org's PRIVATE index
    (its own ingested docs — Drive sync, uploads) is searched alongside the
    avatar's shared base pack and the merged top-k wins; without one, or when
    the org has never ingested anything, behavior is exactly the base pack.
    Isolation is structural: each org's index is its own file, so org A can
    never retrieve org B's documents."""
    # Embed the query ONCE and reuse it for both indexes (base + org).
    qv = np.array(embed([query], input_type="query")[0], dtype=np.float32)
    base = _rank(_load(avatar), query, k, qv)
    org_store = _load_org(avatar, org_id) if org_id else None
    # M2 context scope: a resolved avatar may carry a per-source restriction
    # (avatar_resolver.ResolvedAvatar.context_scope). Applied BEFORE ranking,
    # so out-of-scope chunks never reach the model. Plain avatars have no
    # attribute → behavior byte-identical.
    scope = getattr(avatar, "context_scope", None)
    if org_store is not None and isinstance(scope, dict):
        org_store = _scoped_org_store(org_store, scope)
    if org_store is None:
        return base
    merged = _rank(org_store, query, k, qv) + base
    merged.sort(key=lambda r: -r.score)
    return merged[:k]


def _scoped_org_store(store: dict, scope: dict) -> dict | None:
    """The org store narrowed to a context scope's allowed sources.

    ``include_org_default: true`` means the whole org store (no restriction).
    A RESTRICTED scope with no resolvable sources returns None — an empty
    restriction must never silently widen to \"all sources\". An index file
    written before per-chunk source ids existed also returns None (fail
    closed); the publish path enqueues the rebuild that adds them."""
    if scope.get("include_org_default", True):
        return store
    allowed = {
        str(x).strip().lower()
        for x in (scope.get("knowledge_source_ids") or [])
        if str(x).strip()
    }
    if not allowed:
        return None
    sids = store.get("sids")
    chunks = store.get("chunks") or []
    if not isinstance(sids, list) or len(sids) != len(chunks):
        return None
    keep = [i for i, sid in enumerate(sids)
            if str(sid).strip().lower() in allowed]
    if not keep:
        return None
    if len(keep) == len(chunks):
        return store
    return {
        **store,
        "chunks": [chunks[i] for i in keep],
        "matrix": store["matrix"][keep],
        "sids": [sids[i] for i in keep],
    }


# ─────────────── per-org indexes: an org's OWN ingested docs ────────────────
# The base knowledge pack (avatars/*/knowledge) is the avatar's shared,
# synthetic SOP set. Real customer documents must never land there: they are
# per-tenant. Each (org, avatar) pair gets its own index file next to the
# SQLite store (the persistent mount in production), written by the ingest
# paths (Drive folder sync, file upload) and merged at retrieval above.

# (org_id, avatar.id) -> loaded store
_ORG_CACHE: dict[tuple[str, str], dict] = {}


def _org_slug(org_id: str) -> str:
    """org ids are uuids or u_<hash> — keep the filename strictly safe anyway."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", (org_id or "").strip())


def org_index_path(avatar: Avatar, org_id: str) -> Path:
    from .. import store  # sibling of the SQLite file → survives deploys together

    base = store.STORE_PATH.parent / "org_indexes"
    return base / f"{_org_slug(org_id)}__{avatar.id}.index.json"


def build_org_index_from_chunks(
    avatar: Avatar, org_id: str, chunk_dicts: list[dict]
) -> int:
    """(Re)build one org's private index for one avatar from PRE-CHUNKED
    content — the Company Brain bridge. The durable truth lives in Postgres
    (knowledge_chunks); this writes the same in-memory index format the live
    path ranks, embedding with the CURRENT provider so index and query
    vectors can never disagree. Empty chunk list removes the index."""
    org = (org_id or "").strip()
    if not org:
        raise ValueError("org_id is required for a per-org index")
    path = org_index_path(avatar, org)
    usable = [c for c in chunk_dicts if str(c.get("text") or "").strip()]
    chunks = [
        Chunk(
            text=str(c.get("text") or ""),
            source=str(c.get("source") or ""),
            section=str(c.get("section") or ""),
        )
        for c in usable
    ]
    if not chunks:
        path.unlink(missing_ok=True)
        _ORG_CACHE.pop((org, avatar.id), None)
        _ORG_MISS.pop((org, avatar.id), None)
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same format _write_index produces, plus the additive per-chunk "sids"
    # array (parallel to chunks/vectors) so a context scope can filter per
    # source. Additive: pre-scope readers ignore it; _index_is_current and
    # INDEX_VERSION are untouched.
    vectors = embed([c.text for c in chunks], input_type="document")
    path.write_text(
        json.dumps(
            {
                "provider": provider_signature(),
                "model": settings.embedding_model,
                "version": INDEX_VERSION,
                "sources": [],
                "chunks": [asdict(c) for c in chunks],
                "vectors": vectors,
                "sids": [str(c.get("sid") or "") for c in usable],
            }
        )
    )
    _ORG_CACHE.pop((org, avatar.id), None)  # invalidate
    _ORG_MISS.pop((org, avatar.id), None)  # a fresh ingest is instantly live
    return len(chunks)


def build_org_index(avatar: Avatar, org_id: str, doc_paths: list[Path]) -> int:
    """(Re)build one org's private index for one avatar from ITS documents.
    Empty doc list removes the index (an org disconnecting its sources)."""
    org = (org_id or "").strip()
    if not org:
        raise ValueError("org_id is required for a per-org index")
    path = org_index_path(avatar, org)
    paths = [p for p in doc_paths if p.exists()]
    chunks = _collect_chunks(paths)
    if not chunks:
        path.unlink(missing_ok=True)
        _ORG_CACHE.pop((org, avatar.id), None)
        _ORG_MISS.pop((org, avatar.id), None)
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_index(path, chunks, paths)
    _ORG_CACHE.pop((org, avatar.id), None)  # invalidate
    _ORG_MISS.pop((org, avatar.id), None)  # a fresh ingest is instantly live
    return len(chunks)


# (org_id, avatar.id) -> epoch of a recent miss. retrieve() runs every live
# turn; without this, an org that never ingested anything pays a (cheap but
# pointless) disk stat per turn. A fresh ingest invalidates via build_org_index.
_ORG_MISS: dict[tuple[str, str], float] = {}
_ORG_MISS_TTL = 60.0


def _load_org(avatar: Avatar, org_id: str) -> dict | None:
    org = (org_id or "").strip()
    if not org:
        return None
    key = (org, avatar.id)
    if key not in _ORG_CACHE:
        import time as _time

        missed = _ORG_MISS.get(key)
        if missed and _time.time() - missed < _ORG_MISS_TTL:
            return None
        path = org_index_path(avatar, org)
        if not path.exists():
            _ORG_MISS[key] = _time.time()
            return None
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        # An index from another embedder/format would rank garbage — treat it
        # as absent; the next ingest rewrites it with the current signature.
        if (
            raw.get("provider") != provider_signature()
            or raw.get("model") != settings.embedding_model
            or raw.get("version") != INDEX_VERSION
        ):
            return None
        raw["matrix"] = np.array(raw["vectors"], dtype=np.float32)
        _ORG_CACHE[key] = raw
    return _ORG_CACHE[key]


def _rank(store: dict, query: str, k: int, qv: "np.ndarray | None" = None) -> list[Retrieved]:
    # Accept a PRE-COMPUTED query vector so a single retrieve() embeds the query
    # ONCE and ranks every index (avatar base + org private) with it, instead of
    # re-embedding per index — halves embedding calls (latency + provider rate
    # limit). Callers that pass only the string keep working (embed here).
    if qv is None:
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
