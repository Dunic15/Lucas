"""Embedding provider — pluggable, with a free zero-dependency default.

Pick the provider with EMBEDDING_PROVIDER in .env:

  hash    (default) — deterministic local hashing embedder. No API, no model
                      download, no cost. Approximates keyword-overlap similarity;
                      good enough to demo retrieval over a handful of SOPs.
  local             — real semantic embeddings via `fastembed` (small local
                      ONNX model, free, no API key). `pip install fastembed`.
  voyage            — Voyage AI API (best quality). Needs VOYAGE_API_KEY.
  openai            — OpenAI embeddings (text-embedding-3-small at a fixed
                      512 dimensions — the durable Company Brain default).
                      Needs OPENAI_API_KEY.
  vertex / gemini   — Google Vertex text-embedding-004 (768-dim). Reuses the
                      SAME service account as the Gemini brain (GOOGLE_VERTEX_
                      SA_JSON + VERTEX_PROJECT) — NO new API key, runs on the
                      existing Google/Vertex credits.

All providers expose the same `embed()` so the rest of the code never changes.
"""
from __future__ import annotations

import hashlib
import re

import numpy as np

from ..config import settings

_HASH_DIM = 512
_TOKEN = re.compile(r"[a-z0-9]+")
_fastembed_model = None  # lazy singleton


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 2]


def _hash_embed_one(text: str) -> list[float]:
    """Deterministic bag-of-tokens vector (uses hashlib, not salted hash())."""
    vec = np.zeros(_HASH_DIM, dtype=np.float32)
    for tok in _tokenize(text):
        idx = int(hashlib.md5(tok.encode()).hexdigest(), 16) % _HASH_DIM
        vec[idx] += 1.0
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec.tolist()


def _embed_hash(texts: list[str]) -> list[list[float]]:
    return [_hash_embed_one(t) for t in texts]


# Set once when the local model could not be acquired (e.g. a HuggingFace
# outage at boot — observed 2026-07-16: startup hung on 504s until App
# Runner's health check killed the deploy). Boot must NEVER depend on a
# third-party CDN: we degrade to hash for this process and self-heal on the
# next boot (see provider_signature — the index stamps force a rebuild).
_local_failed = False


def _embed_local(texts: list[str]) -> list[list[float]]:
    global _fastembed_model, _local_failed
    if _local_failed:
        return _embed_hash(texts)
    if _fastembed_model is None:
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            # A missing dependency is a CONFIG error — fail loudly, don't mask.
            raise RuntimeError(
                "EMBEDDING_PROVIDER=local needs fastembed: pip install fastembed"
            ) from e
        try:
            _fastembed_model = TextEmbedding()
        except Exception as e:  # noqa: BLE001 — model download/load failed
            _local_failed = True
            print(
                f"[embeddings] local model unavailable ({type(e).__name__}) — "
                "hash fallback for this process; indexes rebuild automatically "
                "on the next boot with the model",
                flush=True,
            )
            return _embed_hash(texts)
    return [v.tolist() for v in _fastembed_model.embed(texts)]


def provider_signature() -> str:
    """The provider whose vectors embed() ACTUALLY produces right now —
    "hash" when local fell back. Index files stamp THIS (not the configured
    provider), so an index written during an outage mismatches on the next
    healthy boot and rebuilds with real vectors; query and index vectors can
    never silently disagree."""
    provider = settings.embedding_provider.lower()
    if provider == "local" and _local_failed:
        return "hash"
    return provider


def warmup() -> None:
    """Resolve local-model availability NOW (bounded by fastembed's own retry
    budget, ~3min worst case) so boot decides hash-vs-local BEFORE any index
    signature is read — and the live meeting path never pays the download."""
    try:
        embed(["warmup"])
    except Exception:  # noqa: BLE001 — a warmup must never block boot
        pass


def _embed_voyage(texts: list[str], input_type: str) -> list[list[float]]:
    if not settings.voyage_api_key:
        raise RuntimeError("EMBEDDING_PROVIDER=voyage needs VOYAGE_API_KEY.")
    import httpx

    resp = httpx.post(
        "https://api.voyageai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {settings.voyage_api_key}"},
        json={
            "input": texts,
            "model": settings.embedding_model,
            "input_type": input_type,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    data.sort(key=lambda d: d["index"])  # preserve input order
    return [d["embedding"] for d in data]


# Fixed dimension for the OpenAI provider: text-embedding-3-* support native
# dimension truncation server-side. 512 matches the hash provider's size and
# keeps index files/PG rows compact; persist-and-reject lives in the index
# signature (provider_signature + model), never silently mixed.
_OPENAI_DIM = 512


def _embed_openai(texts: list[str]) -> list[list[float]]:
    if not settings.openai_api_key:
        raise RuntimeError("EMBEDDING_PROVIDER=openai needs OPENAI_API_KEY.")
    import httpx

    model = settings.embedding_model
    if not model.startswith("text-embedding-"):
        # embedding_model defaults to a Voyage name; the OpenAI provider needs
        # an OpenAI one — default rather than erroring on the shared field.
        model = "text-embedding-3-small"
    resp = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        json={"input": texts, "model": model, "dimensions": _OPENAI_DIM},
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    data.sort(key=lambda d: d["index"])  # preserve input order
    return [d["embedding"] for d in data]


# Vertex text-embedding: reuses the SAME service-account auth as the Gemini
# brain (llm._vertex_token / _vertex_host), so no new API key — it runs on the
# Google/Vertex credits already configured. text-embedding-004 is 768-dim and
# supports asymmetric task types (RETRIEVAL_DOCUMENT vs RETRIEVAL_QUERY), like
# Voyage's input_type. Batched (Vertex caps instances per predict call).
_VERTEX_EMBED_BATCH = 100


def _embed_vertex(texts: list[str], input_type: str) -> list[list[float]]:
    from . import llm  # lazy: avoid an import cycle at module load

    token = llm._vertex_token()
    project = (settings.vertex_project or "").strip()
    if not (token and project):
        raise RuntimeError(
            "EMBEDDING_PROVIDER=vertex needs GOOGLE_VERTEX_SA_JSON + VERTEX_PROJECT."
        )
    # Embedding models are REGIONAL — never on the 'global' host the brain may
    # use for gemini-3.5-flash. Pin a regional location for embeddings.
    location = (settings.vertex_location or "").strip()
    if not location or location == "global":
        location = "us-central1"
    model = settings.embedding_model
    if not (model.startswith("text-embedding") or model.startswith("gemini-embedding")):
        # embedding_model defaults to a Voyage name; use a Vertex one instead.
        model = "text-embedding-004"
    host = llm._vertex_host(location)
    url = (
        f"https://{host}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model}:predict"
    )
    task = "RETRIEVAL_QUERY" if input_type == "query" else "RETRIEVAL_DOCUMENT"

    import httpx

    # gemini-embedding-001 can emit a chosen dimension via parameters.
    params: dict = {}
    dim = int(getattr(settings, "embedding_dimension", 0) or 0)
    if dim > 0:
        params["outputDimensionality"] = dim

    out: list[list[float]] = []
    for i in range(0, len(texts), _VERTEX_EMBED_BATCH):
        batch = texts[i:i + _VERTEX_EMBED_BATCH]
        body: dict = {"instances": [{"content": t, "task_type": task} for t in batch]}
        if params:
            body["parameters"] = params
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=body,
            timeout=60.0,
        )
        resp.raise_for_status()
        preds = resp.json().get("predictions") or []
        out.extend(p["embeddings"]["values"] for p in preds)
    return out


def embed(texts: list[str], *, input_type: str = "document") -> list[list[float]]:
    """Return one embedding vector per input text.

    input_type ("document" | "query") only matters for providers that support
    asymmetric embeddings (Voyage, Vertex). It is accepted and ignored otherwise.
    """
    if not texts:
        return []

    provider = settings.embedding_provider.lower()
    if provider == "hash":
        return _embed_hash(texts)
    if provider == "local":
        return _embed_local(texts)
    if provider not in ("voyage", "openai", "vertex", "gemini"):
        raise RuntimeError(f"Unknown EMBEDDING_PROVIDER '{provider}'.")
    # HOSTED providers are FAIL-SOFT on the live path: a 429 / outage / missing
    # key must NEVER raise — it would 500 the recall webhook and mute the avatar
    # (live incident 2026-07-22: Vertex 429 → 500 → Petra silent). On any error
    # fall back to the local model (itself hash-safe). Retrieval quality for that
    # one call degrades; the avatar keeps talking. The index-signature rebuild
    # path is unaffected (it stamps the configured provider).
    try:
        if provider == "voyage":
            return _embed_voyage(texts, input_type)
        if provider == "openai":
            return _embed_openai(texts)
        return _embed_vertex(texts, input_type)  # vertex | gemini
    except Exception as e:  # noqa: BLE001 — live path must not raise
        print(
            f"[embeddings] provider {provider!r} failed ({type(e).__name__}) — "
            "local fallback for this call",
            flush=True,
        )
        return _embed_local(texts)
