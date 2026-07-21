"""Health + diagnostics: /health, /health/vendors, /recall/status,
/gmail/status, /gemini-ears/status, /internal/ears-config. Extracted from
main.py. _gmail_state is the SHARED deps dict — never redefine it."""
import hmac
import platform
import time

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import auth, avatars, cedric, gemini_ears, gmail_watcher, graphiti_client, llm, recall_client, store, vendor_health
from ..core.config import settings
from ..brain.engine import effective_provider
from .deps import _gmail_state

router = APIRouter()


@router.get("/gmail/status")
def gmail_status(request: Request) -> JSONResponse:
    """Health of the Gmail 'Add people' auto-join watcher. Gated: `recent_joins`
    carries live meeting URLs + bot_ids (joinable links = PII), so only a
    logged-in owner (or the machine bearer) may read it; never the anonymous
    internet. Open in the key-free demo (auth disabled)."""
    if auth.current_user(request) is None:
        if err := auth.gate(request):
            return err
    has_rt = bool(gmail_watcher.refresh_token())
    last = _gmail_state["last_poll"]
    return JSONResponse(
        {
            "enabled": settings.gmail_watch_enabled,
            "has_refresh_token": has_rt,
            "poll_seconds": settings.gmail_poll_seconds,
            "seconds_since_last_poll": round(time.time() - last, 1) if last else None,
            "last_error": _gmail_state["last_error"],
            "recent_joins": _gmail_state["joined"][-5:],
        }
    )


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "active_sessions": len(store.all_sessions()),
        "brain_provider": effective_provider(),
        "brain_model": settings.brain_model,
        "embedding_provider": settings.embedding_provider,
        "avatars": avatars.list_ids(),
    }


@router.get("/recall/status")
def recall_status(check_auth: bool = False) -> JSONResponse:
    """Report Recall setup state without returning any secret values."""
    status = recall_client.auth_check() if check_auth else recall_client.readiness()
    return JSONResponse(status, status_code=200 if status["ready"] else 400)


@router.get("/health/vendors")
def vendors_view(request: Request) -> JSONResponse:
    """On-demand vendor subscription/credit sweep (same checks as the daily
    Slack watchdog). Auth-gated: statuses reveal which vendors are configured."""
    if err := cedric.auth_error(request):  # CEDRIC
        return err
    results = vendor_health.run_checks()
    return JSONResponse(
        {
            "checked_at": vendor_health.last_run_at,
            "vendors": results,
            "alert": vendor_health.slack_text(results) or "tutto ok",
        }
    )


@router.get("/health/graphiti")
async def graphiti_health(request: Request, run: int = 0) -> JSONResponse:
    """Knowledge-graph (Graphiti) status, and; with ?run=1; a LIVE ingest→recall
    smoke test against the configured Neo4j using the Anthropic extraction LLM +
    Laura's local embedder. Auth-gated (statuses reveal what's configured). The
    live round-trip writes to a dedicated ``__smoke__`` group, so it never
    touches a real org's graph.

    ``python`` + ``graphiti_core`` in the response settle the 2026-07-20 deploy
    incident (#319) in one curl: graphiti-core has NO release for Python <3.10,
    so on an old runtime the dep is marker-skipped and reads MISSING here.
    First-activation check: GET this with ?run=1 after wiring GRAPHITI_*: 
    ``live.ok:true`` means ingest wrote and recall read facts back."""
    if err := cedric.auth_error(request):  # CEDRIC
        return err
    info: dict = {
        "enabled": graphiti_client.enabled(),
        "configured": bool(settings.graphiti_enabled and settings.graphiti_uri.strip()),
        "uri_set": bool(settings.graphiti_uri.strip()),
        "backend": settings.graphiti_backend,
        "llm_model": settings.graphiti_llm_model,
        "small_model": settings.graphiti_llm_small_model,
        "embedding_dim": settings.graphiti_embedding_dim,
        "anthropic_key_set": bool(settings.anthropic_api_key.strip()),
        "python": platform.python_version(),
    }
    try:
        import graphiti_core  # type: ignore
        info["graphiti_core"] = getattr(graphiti_core, "__version__", "installed")
    except Exception as e:  # noqa: BLE001
        info["graphiti_core"] = f"MISSING ({type(e).__name__})"
    if not run:
        return JSONResponse(info)
    if not graphiti_client.enabled():
        info["live"] = {"ok": False, "stage": "disabled",
                        "hint": "set GRAPHITI_ENABLED + GRAPHITI_URI/USER/PASSWORD"}
        return JSONResponse(info, status_code=503)

    org = "__smoke__"
    sample = (
        "Smoke check: issue ENG-999 'Wire the payments webhook' is assigned to "
        "Dana Lin and is blocked by ENG-1000. It is in the current sprint, "
        "Sprint 42, which ends on Friday."
    )
    t0 = time.monotonic()
    ready = await graphiti_client.ensure_ready()
    t_connect = round(time.monotonic() - t0, 2)
    if not ready:
        info["live"] = {"ok": False, "stage": "connect", "connect_s": t_connect,
                        "hint": "init failed — check server logs for '[graphiti] "
                                "disabled' (creds/URI/graphiti-core)"}
        return JSONResponse(info, status_code=503)
    t1 = time.monotonic()
    ingest_ok = await graphiti_client.ingest(
        org, sample, name="smoke", source_description="smoke")
    t_ingest = round(time.monotonic() - t1, 2)
    t2 = time.monotonic()
    facts = await graphiti_client.recall(
        org, "who is ENG-999 assigned to and what is blocking it",
        timeout_s=20.0, num_results=8)
    t_recall = round(time.monotonic() - t2, 2)
    lines = [ln for ln in facts.splitlines() if ln.strip()]
    info["live"] = {
        "ok": bool(ingest_ok and lines),
        "connect_s": t_connect,
        "ingest_ok": ingest_ok,
        "ingest_s": t_ingest,
        "recall_s": t_recall,
        "recall_count": len(lines),
        "recall_facts": lines,
    }
    return JSONResponse(info)


@router.get("/gemini-ears/status")
def gemini_ears_status() -> JSONResponse:
    """PII-safe ears telemetry: counts and timing only, never content."""
    return JSONResponse(gemini_ears.status())


@router.get("/internal/ears-config/{capability}")
async def ears_config(capability: str, request: Request) -> JSONResponse:
    """Per-session config + a fresh Vertex token for the Cloudflare ears relay.

    The relay (which accepts Recall's audio WS that App Runner can't) calls this
    with the per-bot capability to learn the mode/model/persona and get a token
    to open the Gemini Live session. Bearer-protected with LAURA_API_TOKEN; the
    capability itself binds the response to exactly one bot. No transcript/PII
    here; only config + a short-lived token.
    """
    expected = settings.laura_api_token.strip()
    got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not expected or not hmac.compare_digest(got, expected):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    bot_id = await run_in_threadpool(
        store.resolve_recall_realtime_capability, capability
    )
    if not bot_id:
        return JSONResponse({"error": "invalid capability"}, status_code=404)
    session = store.get(bot_id)
    persona = "Laura"
    avatar_id = session.avatar_id if session is not None else ""
    if session is not None:
        try:
            persona = avatars.load(avatar_id).name
        except Exception:  # noqa: BLE001
            pass
    # Per-avatar brain choice: this avatar may be on Cerebras (ears off) even if
    # another is on Gemini. Resolve AFTER the bot so we know which avatar it is.
    _mode = gemini_ears.mode_for_avatar(avatar_id)
    if not gemini_ears.mode_enabled(_mode):
        return JSONResponse({"enabled": False, "reason": "brain not gemini"})
    try:
        token = await run_in_threadpool(llm._vertex_token)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"token: {type(e).__name__}"}, status_code=502)
    # Prime the light per-bot state so the ring/attribution exist immediately.
    gemini_ears._ensure_state(bot_id, capability, persona)
    return JSONResponse(
        {
            "enabled": True,
            "mode": _mode,
            "bot_id": bot_id,
            "project": settings.vertex_project,
            "location": settings.vertex_location or "us-central1",
            "live_model": settings.vertex_live_model,
            "persona": persona,
            "vertex_token": token,
        }
    )
