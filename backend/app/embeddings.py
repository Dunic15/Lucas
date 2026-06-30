"""Embedding provider.

Default: Voyage AI (Anthropic's recommended embedding pairing). Swappable —
implement the same `embed()` signature for OpenAI, a local model, etc.
"""
from __future__ import annotations

import httpx

from .config import settings

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


def embed(texts: list[str], *, input_type: str = "document") -> list[list[float]]:
    """Return one embedding vector per input text.

    input_type: "document" when indexing, "query" when retrieving — Voyage
    uses this to asymmetrically tune the embedding space.
    """
    if not settings.voyage_api_key:
        raise RuntimeError(
            "VOYAGE_API_KEY is not set — needed to embed process docs for RAG."
        )
    if not texts:
        return []

    resp = httpx.post(
        VOYAGE_URL,
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
    # Voyage returns objects with an "index" — sort to preserve input order.
    data.sort(key=lambda d: d["index"])
    return [d["embedding"] for d in data]
