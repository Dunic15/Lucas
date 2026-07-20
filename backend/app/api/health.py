"""Health + diagnostics: /health, /health/vendors, /recall/status,
/gmail/status, /gemini-ears/status, /internal/ears-config. Extracted from
main.py. _gmail_state is the SHARED deps dict — never redefine it."""
import hmac
import time

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import auth, avatars, cedric, gemini_ears, gmail_watcher, llm, recall_client, store, vendor_health
from app.core.config import settings
from app.brain.engine import effective_provider
from app.api.deps import _gmail_state

router = APIRouter()


@router.get("/gmail/status")
def gmail_status(request: Request) -> JSONResponse:
    """Health of the Gmail 'Add people' auto-join watcher. Gated: `recent_joins`
    carries live meeting URLs + bot_ids (joinable links = PII), so only a
    logged-in owner (or the machine bearer) may read it — never the anonymous
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
    here — only config + a short-lived token.
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
