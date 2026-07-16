"""Embedding provider — pluggable, with a free zero-dependency default.

Pick the provider with EMBEDDING_PROVIDER in .env:

  hash    (default) — deterministic local hashing embedder. No API, no model
                      download, no cost. Approximates keyword-overlap similarity;
                      good enough to demo retrieval over a handful of SOPs.
  local             — real semantic embeddings via `fastembed` (small local
                      ONNX model, free, no API key). `pip install fastembed`.
  voyage            — Voyage AI API (best quality). Needs VOYAGE_API_KEY.

All providers expose the same `embed()` so the rest of the code never changes.
"""
from __future__ import annotations

import hashlib
import re

import numpy as np

from .config import settings

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


def embed(texts: list[str], *, input_type: str = "document") -> list[list[float]]:
    """Return one embedding vector per input text.

    input_type ("document" | "query") only matters for providers that support
    asymmetric embeddings (Voyage). It is accepted and ignored otherwise.
    """
    if not texts:
        return []

    provider = settings.embedding_provider.lower()
    if provider == "hash":
        return _embed_hash(texts)
    if provider == "local":
        return _embed_local(texts)
    if provider == "voyage":
        return _embed_voyage(texts, input_type)
    raise RuntimeError(f"Unknown EMBEDDING_PROVIDER '{provider}'.")
