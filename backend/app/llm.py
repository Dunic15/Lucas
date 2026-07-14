"""LLM provider — pluggable, with a free local option.

Pick the provider with BRAIN_PROVIDER in .env:

  anthropic (default) — Claude (best quality). Needs ANTHROPIC_API_KEY. With no
                      key present, brain.effective_provider() transparently falls
                      back to `stub` so the demo still runs.
  cerebras          — FASTEST live inference (~0.17s first token). Same
                      OpenAI-compatible wire format as Groq, different endpoint.
                      Streams + supports tool use. Needs CEREBRAS_API_KEY. This
                      is what prod runs on the live spoken path.
  groq              — fast, cheap open models via an OpenAI-compatible API.
                      Streams + supports tool use. Needs GROQ_API_KEY.
  vertex            — Google Gemini via Vertex AI (GCP-billed, so it can run on
                      Google Cloud credits — unlike the AI Studio Gemini API).
                      Auth via a service account / ADC (google-auth). Set
                      VERTEX_PROJECT (+ optional VERTEX_LOCATION/VERTEX_MODEL).
                      Text brain only here; realtime voice is a separate spike.
  ollama            — a local model via Ollama (free, runs on your machine).
                      `ollama run llama3.2` then BRAIN_PROVIDER=ollama.
  stub              — no model at all. Deterministic, offline, zero-cost. Used to
                      prove the end-to-end pipeline for free; the stub answers
                      live in brain.py (they need the retrieved chunks), so this
                      module only handles *real* models.

`complete()` returns the model's raw text. brain.py prompts for JSON and parses.
"""
from __future__ import annotations

import time
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


# Resilient fallback model: if the primary provider fails (e.g. Groq
# decommissions a model overnight, or rate-limits), we drop to Claude Haiku so
# the avatar never goes dark. Anthropic key is already provisioned in prod.
_FALLBACK_MODEL = "claude-haiku-4-5"


# ── OpenAI-compatible fast providers ───────────────────────────────────
# Groq and Cerebras speak the identical OpenAI /chat/completions wire format —
# only the endpoint + API key differ. One implementation (_complete_groq /
# _stream_groq / complete_with_tools) serves both; _compat_creds() picks the
# right base URL + key for the resolved provider.
_OPENAI_COMPAT = {"groq", "cerebras"}


def _compat_creds(provider: str) -> tuple[str, str]:
    """(base_url, api_key) for an OpenAI-compatible provider (groq | cerebras)."""
    if provider == "cerebras":
        if not settings.cerebras_api_key:
            raise RuntimeError("BRAIN_PROVIDER=cerebras needs CEREBRAS_API_KEY.")
        return settings.cerebras_base, settings.cerebras_api_key
    if not settings.groq_api_key:
        raise RuntimeError("BRAIN_PROVIDER=groq needs GROQ_API_KEY.")
    return settings.groq_base, settings.groq_api_key


# ── fast-provider circuit breaker ──────────────────────────────────────
# Groq/Cerebras rate-limit (HTTP 429) under load. Rather than hammer them on
# every request — eating the failure + fallback latency each time — we trip a
# breaker on failure: for a cooldown window (the 429's Retry-After if present,
# else a default) live answers skip the fast provider entirely and go straight
# to Claude Haiku. The breaker is process-local and self-heals when the window
# elapses. (Names keep the `groq` prefix for backward compatibility with tests
# and callers; the breaker is provider-agnostic — a single timestamp gate.)
_GROQ_COOLDOWN_DEFAULT = 30.0
_GROQ_COOLDOWN_MAX = 300.0
_groq_blocked_until = 0.0  # monotonic timestamp; 0 = breaker closed


def _groq_breaker_open() -> bool:
    """True while the breaker is tripped — skip Groq, use the Haiku fallback."""
    return time.monotonic() < _groq_blocked_until


def _trip_groq_breaker(exc: Exception) -> None:
    """Open the breaker after a Groq failure, honouring Retry-After on a 429."""
    global _groq_blocked_until
    cooldown = _GROQ_COOLDOWN_DEFAULT
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        retry_after = (getattr(resp, "headers", {}) or {}).get("retry-after")
        if retry_after:
            try:
                cooldown = max(cooldown, float(retry_after))
            except (TypeError, ValueError):
                pass  # Retry-After can be an HTTP-date; the default covers it
    cooldown = min(cooldown, _GROQ_COOLDOWN_MAX)
    _groq_blocked_until = time.monotonic() + cooldown
    print(
        f"[llm] fast-provider breaker OPEN {cooldown:.0f}s ({exc}); "
        f"routing live answers to {_FALLBACK_MODEL}",
        flush=True,
    )


def _reset_groq_breaker() -> None:
    """Close the breaker immediately (used by tests)."""
    global _groq_blocked_until
    _groq_blocked_until = 0.0


# ── Vertex AI (Google Gemini) ──────────────────────────────────────────
# Gemini via Vertex AI (GCP-billed — funded by Google Cloud credits, unlike the
# AI Studio Gemini API which the GCP Free Trial does NOT cover). Plain REST
# generateContent with a bearer minted from Application Default Credentials / a
# service account (google-auth, lazy-imported so the rest of the app and the
# tests never need it). Opt-in via BRAIN_PROVIDER=vertex — the live spoken path
# stays on cerebras/anthropic. Realtime *voice* (gemini-live-2.5-flash over a
# websocket) is a separate concern and lives in the standalone spike, never here.
#
# Model/endpoint note (verified 2026-07-14): gemini-2.5-flash works on both a
# region host and the `global` host; gemini-3.5-flash is `global`-only. So the
# host is derived from vertex_location: "global" -> aiplatform.googleapis.com,
# else "<loc>-aiplatform.googleapis.com".
_vertex_token_cache = {"tok": "", "exp": 0.0}


def _vertex_token() -> str:
    """Mint (and cache) a Vertex access token via google-auth ADC / SA JSON.

    Set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON with the role
    roles/aiplatform.user, or rely on ambient Application Default Credentials.
    Cached until ~1 min before expiry.
    """
    now = time.time()
    if _vertex_token_cache["tok"] and now < _vertex_token_cache["exp"] - 60:
        return _vertex_token_cache["tok"]
    try:
        import google.auth
        from google.auth.transport.requests import Request
    except ImportError as e:  # pragma: no cover - depends on optional dep
        raise RuntimeError(
            "BRAIN_PROVIDER=vertex needs google-auth (pip install google-auth)."
        ) from e
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(Request())
    exp = getattr(creds, "expiry", None)
    if exp is not None:
        import calendar

        ttl_exp = float(calendar.timegm(exp.timetuple()))
    else:
        ttl_exp = now + 3000.0  # tokens live ~1h; be conservative
    _vertex_token_cache.update(tok=creds.token, exp=ttl_exp)
    return creds.token


def _vertex_host(location: str) -> str:
    return (
        "aiplatform.googleapis.com"
        if location == "global"
        else f"{location}-aiplatform.googleapis.com"
    )


def _complete_vertex(
    system: str, user: str, max_tokens: int, model: str | None = None
) -> str:
    import httpx

    if not settings.vertex_project:
        raise RuntimeError(
            "BRAIN_PROVIDER=vertex needs VERTEX_PROJECT (the GCP project id)."
        )
    model = model or settings.vertex_model
    loc = settings.vertex_location or "global"
    url = (
        f"https://{_vertex_host(loc)}/v1/projects/{settings.vertex_project}"
        f"/locations/{loc}/publishers/google/models/{model}:generateContent"
    )
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    resp = httpx.post(
        url,
        json=body,
        headers={
            "Authorization": f"Bearer {_vertex_token()}",
            "Content-Type": "application/json",
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    cands = data.get("candidates") or []
    if not cands:
        return ""
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if "text" in p)


def _dispatch_complete(
    provider: str, system: str, user: str, max_tokens: int, model: str | None
) -> str:
    if provider == "anthropic":
        return _complete_anthropic(system, user, max_tokens, model)
    if provider in _OPENAI_COMPAT:
        return _complete_groq(system, user, max_tokens, model, provider=provider)
    if provider == "vertex":
        return _complete_vertex(system, user, max_tokens, model)
    if provider == "ollama":
        return _complete_ollama(system, user, max_tokens)
    if provider == "stub":
        raise RuntimeError(
            "BRAIN_PROVIDER=stub does not call an LLM — brain.py handles it. "
            "This path should not be reached."
        )
    raise RuntimeError(f"Unknown BRAIN_PROVIDER '{provider}'.")


def complete(
    system: str, user: str, *, max_tokens: int = 800, model: str | None = None,
    provider: str | None = None,
) -> str:
    resolved = (provider or settings.brain_provider).lower()
    # Circuit breaker: while the fast provider is in cooldown, skip it -> Haiku.
    if resolved in _OPENAI_COMPAT and _groq_breaker_open() and settings.anthropic_api_key:
        return _complete_anthropic(system, user, max_tokens, _FALLBACK_MODEL)
    try:
        return _dispatch_complete(resolved, system, user, max_tokens, model)
    except Exception as e:  # noqa: BLE001
        if resolved in _OPENAI_COMPAT:
            _trip_groq_breaker(e)
        # Only auto-fall-back for the DEFAULT live/post path. An explicit provider=
        # (e.g. the native web_search call) has its own handling and must not be
        # silently answered by a non-searching model.
        if provider is None and resolved != "anthropic" and settings.anthropic_api_key:
            print(f"[llm] {resolved} failed ({e}); falling back to {_FALLBACK_MODEL}", flush=True)
            return _complete_anthropic(system, user, max_tokens, _FALLBACK_MODEL)
        raise


# Non-streaming completions prompt for a compact JSON object (post-meeting
# artifact, classification, extraction). Sonnet-5's adaptive extended thinking
# would otherwise consume the max_tokens budget BEFORE emitting the answer,
# returning truncated or empty text that the caller reads as a degraded result
# (the empty-summary bug). Disabling thinking spends the whole budget on the
# JSON. "disabled" is the only valid shape here — passing budget_tokens 400s on
# Sonnet-5. The live SPOKEN path uses stream_complete/_stream_anthropic and is
# unaffected.
_THINKING_OFF = {"type": "disabled"}
_thinking_supported = True  # flipped off if a model ever rejects the param


def _complete_anthropic(
    system: str, user: str, max_tokens: int, model: str | None = None
) -> str:
    global _thinking_supported
    client = _ensure_anthropic()
    model = model or settings.brain_model
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if _thinking_supported:
        kwargs["thinking"] = _THINKING_OFF
    try:
        msg = client.messages.create(**kwargs)
    except Exception as e:  # noqa: BLE001
        # A model/SDK that rejects the thinking param: retry once without it
        # rather than fail the whole completion (and stop sending it thereafter).
        if _thinking_supported and "thinking" in str(e).lower():
            _thinking_supported = False
            kwargs.pop("thinking", None)
            msg = client.messages.create(**kwargs)
        else:
            raise
    if getattr(msg, "stop_reason", None) == "max_tokens":
        # Truncated before the model finished — the JSON is likely invalid, which
        # the caller degrades to a deterministic recap. Log so it's diagnosable.
        print(
            f"[llm] anthropic {model} hit max_tokens={max_tokens}; "
            "output may be truncated",
            flush=True,
        )
    # Models with adaptive thinking (Sonnet 5+) put a thinking block FIRST —
    # content[0] is not necessarily text. Return the first text block.
    return next((b.text for b in msg.content if b.type == "text"), "")


def stream_complete(
    system: str, user: str, *, max_tokens: int = 800, model: str | None = None,
    provider: str | None = None,
) -> Iterator[str]:
    """Yield the model's answer as text deltas, for low-latency spoken output.

    Anthropic streams token-by-token. Other providers have no streaming path here,
    so they yield the full answer as a single chunk (still correct, just not early).
    `provider` overrides BRAIN_PROVIDER for this call (tiered routing picks it).
    """
    provider = (provider or settings.brain_provider).lower()
    if provider == "anthropic":
        yield from _stream_anthropic(system, user, max_tokens, model)
        return
    # Circuit breaker: while the fast provider is in cooldown after a 429, don't
    # even try it — stream Haiku directly so the spoken path never goes silent.
    if provider in _OPENAI_COMPAT and _groq_breaker_open() and settings.anthropic_api_key:
        yield from _stream_anthropic(system, user, max_tokens, _FALLBACK_MODEL)
        return
    # Non-anthropic primary (cerebras/groq/…): if it fails BEFORE producing any
    # output (e.g. the model was decommissioned), fall back to Claude Haiku so she
    # never goes silent. If it already streamed something, don't fall back (dupes).
    yielded = False
    try:
        if provider in _OPENAI_COMPAT:
            for text in _stream_groq(system, user, max_tokens, model, provider=provider):
                yielded = True
                yield text
        else:
            yielded = True
            yield complete(system, user, max_tokens=max_tokens, model=model)
    except Exception as e:  # noqa: BLE001
        if provider in _OPENAI_COMPAT:
            _trip_groq_breaker(e)
        if yielded or not settings.anthropic_api_key:
            raise
        print(f"[llm] stream {provider} failed ({e}); falling back to {_FALLBACK_MODEL}", flush=True)
        yield from _stream_anthropic(system, user, max_tokens, _FALLBACK_MODEL)


def web_search(
    system: str, user: str, *, model: str, max_tokens: int = 2048, max_rounds: int = 4
) -> str:
    """Answer using Claude's native server-side web_search tool.

    Returns the final spoken text, or "" if nothing came back. The tool runs on
    Anthropic's side (no client execution); the server may pause after its own tool
    rounds with stop_reason 'pause_turn' — re-send the turn to continue. `web_search`
    with dynamic filtering needs a capable model (Sonnet/Opus); the older basic tool
    is used automatically for smaller models via the try/except fallback below.
    """
    client = _ensure_anthropic()

    def _run(tool_type: str) -> str:
        messages: list[dict] = [{"role": "user", "content": user}]
        msg = None
        for _ in range(max_rounds):
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=[{"type": tool_type, "name": "web_search"}],
            )
            if msg.stop_reason == "pause_turn":
                # Server hit its tool-round limit — resend to let it continue.
                messages = [
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": msg.content},
                ]
                continue
            break
        return "".join(
            b.text for b in (msg.content if msg else []) if getattr(b, "type", None) == "text"
        ).strip()

    # Dynamic-filtering tool (web_search_20260209) needs Sonnet/Opus; Haiku and
    # smaller models use the basic web_search_20250305. Pick the right one first so
    # we don't waste a round-trip, and fall back to the other on error.
    is_big = any(m in (model or "").lower() for m in ("sonnet", "opus"))
    primary = "web_search_20260209" if is_big else "web_search_20250305"
    fallback = "web_search_20250305" if is_big else "web_search_20260209"
    try:
        return _run(primary)
    except Exception as e:  # noqa: BLE001
        print(f"[search] {primary} failed ({e}); trying {fallback}", flush=True)
        try:
            return _run(fallback)
        except Exception as e2:  # noqa: BLE001
            print(f"[search] {fallback} failed ({e2})", flush=True)
            return ""


def _stream_anthropic(
    system: str, user: str, max_tokens: int, model: str | None
) -> Iterator[str]:
    client = _ensure_anthropic()
    # Cache the (byte-identical) system prompt so repeat calls skip re-processing
    # it. Note: Haiku's minimum cacheable prefix is 4096 tokens — if the persona
    # prompt is shorter, this silently won't cache (harmless).
    with client.messages.stream(
        model=model or settings.brain_model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    ) as stream:
        for text in stream.text_stream:
            yield text
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


def _groq_messages(system: str, user: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _complete_groq(
    system: str, user: str, max_tokens: int, model: str | None = None,
    provider: str = "groq",
) -> str:
    """Non-streaming OpenAI-compatible call (groq | cerebras). Post-meeting path."""
    import httpx

    base, key = _compat_creds(provider)
    resp = httpx.post(
        f"{base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
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
    system: str, user: str, max_tokens: int, model: str | None = None,
    provider: str = "groq",
) -> Iterator[str]:
    """Stream text deltas from an OpenAI-compatible provider (groq | cerebras)."""
    import json as _json

    import httpx

    base, key = _compat_creds(provider)
    with httpx.stream(
        "POST",
        f"{base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
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

    Runs the OpenAI function-calling loop: the model may ask to call tools; we
    execute each via `dispatch(name, args)`, feed the results back, and let it
    answer. Wired for the OpenAI-compatible providers (cerebras | groq). For other
    providers — or while the fast-provider breaker is open — we fall back to a
    normal completion with no tools (which itself routes to Haiku), so nothing
    breaks: the caller still gets a sensible answer, just without acting.
    """
    provider = settings.brain_provider.lower()
    if provider not in _OPENAI_COMPAT or _groq_breaker_open():
        return complete(system, user, max_tokens=max_tokens, model=model), []

    import json as _json

    import httpx

    base, key = _compat_creds(provider)
    url = f"{base.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    messages: list[dict] = _groq_messages(system, user)
    used: list[dict] = []

    try:
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
    except Exception as e:  # noqa: BLE001 — trip the breaker so the caller's plain
        _trip_groq_breaker(e)  # -answer fallback routes to Haiku, not back to Groq
        raise


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
