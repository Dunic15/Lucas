"""LLM provider — pluggable, with a free local option.

Pick the provider with BRAIN_PROVIDER in .env:

  stub    (default) — no model at all. Deterministic, offline, zero-cost.
                      Used to prove the end-to-end pipeline for free; the
                      stub answers live in brain.py (they need the retrieved
                      chunks), so this module only handles *real* models.
  ollama            — a local model via Ollama (free, runs on your machine).
                      `ollama run llama3.2` then BRAIN_PROVIDER=ollama.
  anthropic         — Claude (best quality). Needs ANTHROPIC_API_KEY.

`complete()` returns the model's raw text. brain.py prompts for JSON and parses.
"""
from __future__ import annotations

from typing import Iterator

from .config import settings

_anthropic_client = None  # lazy singleton


def _ensure_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("BRAIN_PROVIDER=anthropic needs ANTHROPIC_API_KEY.")
        _anthropic_client = Anthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


def complete(
    system: str, user: str, *, max_tokens: int = 800, model: str | None = None
) -> str:
    provider = settings.brain_provider.lower()
    if provider == "anthropic":
        return _complete_anthropic(system, user, max_tokens, model)
    if provider == "ollama":
        return _complete_ollama(system, user, max_tokens)
    if provider == "stub":
        raise RuntimeError(
            "BRAIN_PROVIDER=stub does not call an LLM — brain.py handles it. "
            "This path should not be reached."
        )
    raise RuntimeError(f"Unknown BRAIN_PROVIDER '{provider}'.")


def _complete_anthropic(
    system: str, user: str, max_tokens: int, model: str | None = None
) -> str:
    client = _ensure_anthropic()
    msg = client.messages.create(
        model=model or settings.brain_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def stream_complete(
    system: str, user: str, *, max_tokens: int = 800, model: str | None = None
) -> Iterator[str]:
    """Yield the model's answer as text deltas, for low-latency spoken output.

    Anthropic streams token-by-token. Other providers have no streaming path here,
    so they yield the full answer as a single chunk (still correct, just not early).
    """
    provider = settings.brain_provider.lower()
    if provider == "anthropic":
        client = _ensure_anthropic()
        with client.messages.stream(
            model=model or settings.brain_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            for text in stream.text_stream:
                yield text
        return
    # Fallback: no incremental streaming for this provider.
    yield complete(system, user, max_tokens=max_tokens, model=model)


def _complete_ollama(system: str, user: str, max_tokens: int) -> str:
    import httpx

    resp = httpx.post(
        f"{settings.ollama_host.rstrip('/')}/api/chat",
        json={
            "model": settings.ollama_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"num_predict": max_tokens},
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]
