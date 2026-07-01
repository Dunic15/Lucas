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


def _embed_local(texts: list[str]) -> list[list[float]]:
    global _fastembed_model
    if _fastembed_model is None:
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=local needs fastembed: pip install fastembed"
            ) from e
        _fastembed_model = TextEmbedding()
    return [v.tolist() for v in _fastembed_model.embed(texts)]


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
