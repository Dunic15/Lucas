"""LLM provider — pluggable, with a free local option.

Pick the provider with BRAIN_PROVIDER in .env:

  anthropic (default) — Claude (best quality). Needs ANTHROPIC_API_KEY. With no
                      key present, brain.effective_provider() transparently falls
                      back to `stub` so the demo still runs.
  groq              — fast, cheap open models via an OpenAI-compatible API.
                      Streams + supports tool use. Needs GROQ_API_KEY.
  ollama            — a local model via Ollama (free, runs on your machine).
                      `ollama run llama3.2` then BRAIN_PROVIDER=ollama.
  stub              — no model at all. Deterministic, offline, zero-cost. Used to
                      prove the end-to-end pipeline for free; the stub answers
                      live in brain.py (they need the retrieved chunks), so this
                      module only handles *real* models.

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
    system: str, user: str, *, max_tokens: int = 800, model: str | None = None,
    provider: str | None = None,
) -> str:
    provider = (provider or settings.brain_provider).lower()
    if provider == "anthropic":
        return _complete_anthropic(system, user, max_tokens, model)
    if provider == "groq":
        return _complete_groq(system, user, max_tokens, model)
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
    # Models with adaptive thinking (Sonnet 5+) put a thinking block FIRST —
    # content[0] is not necessarily text. Return the first text block.
    return next((b.text for b in msg.content if b.type == "text"), "")


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
        # Cache the (byte-identical) system prompt so repeat calls skip
        # re-processing it. Note: Haiku's minimum cacheable prefix is 4096 tokens
        # — if the persona prompt is shorter, this silently won't cache (harmless);
        # the usage log below tells us whether it engaged.
        with client.messages.stream(
            model=model or settings.brain_model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user}],
        ) as stream:
            for text in stream.text_stream:
                yield text
            # After the stream drains, log token usage so we can see the real
            # input size and whether the system-prompt cache is being hit.
            try:
                u = stream.get_final_message().usage
                print(
                    f"[latency] llm usage input={u.input_tokens} "
                    f"cache_read={getattr(u, 'cache_read_input_tokens', 0)} "
                    f"cache_write={getattr(u, 'cache_creation_input_tokens', 0)} "
                    f"output={u.output_tokens}",
                    flush=True,
                )
            except Exception:
                pass
        return
    if provider == "groq":
        yield from _stream_groq(system, user, max_tokens, model)
        return
    # Fallback: no incremental streaming for this provider.
    yield complete(system, user, max_tokens=max_tokens, model=model)


def _groq_messages(system: str, user: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _complete_groq(
    system: str, user: str, max_tokens: int, model: str | None = None
) -> str:
    """Non-streaming Groq call (OpenAI-compatible). Used for post-meeting artifacts."""
    import httpx

    if not settings.groq_api_key:
        raise RuntimeError("BRAIN_PROVIDER=groq needs GROQ_API_KEY.")
    resp = httpx.post(
        f"{settings.groq_base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        json={
            "model": model or settings.brain_model,
            "max_tokens": max_tokens,
            "messages": _groq_messages(system, user),
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _stream_groq(
    system: str, user: str, max_tokens: int, model: str | None = None
) -> Iterator[str]:
    """Stream text deltas from Groq (OpenAI-compatible SSE) for low-latency speech."""
    import json as _json

    import httpx

    if not settings.groq_api_key:
        raise RuntimeError("BRAIN_PROVIDER=groq needs GROQ_API_KEY.")
    with httpx.stream(
        "POST",
        f"{settings.groq_base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        json={
            "model": model or settings.brain_model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": _groq_messages(system, user),
        },
        timeout=120.0,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                delta = _json.loads(data)["choices"][0]["delta"].get("content")
            except (KeyError, IndexError, ValueError):
                continue
            if delta:
                yield delta


def complete_with_tools(
    system: str,
    user: str,
    tools: list[dict],
    dispatch,
    *,
    max_tokens: int = 600,
    model: str | None = None,
    max_rounds: int = 3,
) -> tuple[str, list[dict]]:
    """Answer WITH tool use (the 'act' layer). Returns (final_text, tools_used).

    Runs the OpenAI/Groq function-calling loop: the model may ask to call tools;
    we execute each via `dispatch(name, args)`, feed the results back, and let it
    answer. Only wired for Groq today (OpenAI-compatible). For other providers we
    fall back to a normal completion with no tools, so nothing breaks — the caller
    still gets a sensible answer, just without acting.
    """
    provider = settings.brain_provider.lower()
    if provider != "groq":
        return complete(system, user, max_tokens=max_tokens, model=model), []

    import json as _json

    import httpx

    if not settings.groq_api_key:
        raise RuntimeError("BRAIN_PROVIDER=groq needs GROQ_API_KEY.")

    url = f"{settings.groq_base.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
    messages: list[dict] = _groq_messages(system, user)
    used: list[dict] = []

    with httpx.Client(timeout=120.0) as client:
        for _ in range(max_rounds):
            resp = client.post(
                url,
                headers=headers,
                json={
                    "model": model or settings.brain_model,
                    "max_tokens": max_tokens,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                },
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                return (msg.get("content") or "").strip(), used

            # Record the assistant turn that requested tools, then run each call.
            messages.append(
                {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls}
            )
            for tc in tool_calls:
                fn = tc.get("function", {}).get("name", "")
                try:
                    args = _json.loads(tc.get("function", {}).get("arguments") or "{}")
                except ValueError:
                    args = {}
                result = dispatch(fn, args)
                used.append({"tool": fn, "args": args, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "name": fn,
                        "content": str(result),
                    }
                )

        # Out of rounds — force a final answer with the tool results in hand.
        resp = client.post(
            url,
            headers=headers,
            json={
                "model": model or settings.brain_model,
                "max_tokens": max_tokens,
                "messages": messages,
            },
        )
        resp.raise_for_status()
        return (resp.json()["choices"][0]["message"].get("content") or "").strip(), used


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
