"""/dashboard/connections/pipedream/* — connect any app through Pipedream.

The dashboard's door to the OpenClaw action plane's breadth layer: search the
Pipedream catalog, mint a connect link for any app, list what's connected,
and sync connections into the local mcporter config so the OpenClaw agent
picks them up as tools. Same auth gate as the rest of the dashboard.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import auth
from ..config import settings
from ..integrations import pipedream_client

router = APIRouter(tags=["pipedream-connect"])


def _gate(request: Request) -> JSONResponse | None:
    if not pipedream_client.enabled():
        return JSONResponse(
            {"ok": False, "error": "pipedream not configured"}, status_code=503
        )
    return auth.gate(request)


@router.get("/dashboard/connections/pipedream/apps")
def pipedream_apps(request: Request, q: str = "") -> JSONResponse:
    """Search the app catalog (the "connect as many as you want" picker)."""
    if err := _gate(request):
        return err
    return JSONResponse(pipedream_client.search_apps(q))


@router.get("/dashboard/connections/pipedream/accounts")
def pipedream_accounts(request: Request) -> JSONResponse:
    """Connected accounts + a best-effort mcporter sync, so simply reloading
    the dashboard after an OAuth completes is enough to arm the agent."""
    if err := _gate(request):
        return err
    result = pipedream_client.list_accounts()
    if result.get("ok") and settings.openclaw_executor:
        result["sync"] = pipedream_client.sync_mcporter()
    return JSONResponse(result)


@router.post("/dashboard/connections/pipedream/connect")
async def pipedream_connect(request: Request) -> JSONResponse:
    """Mint a connect link for one app. Body: {"app": "<slug>"}."""
    if err := _gate(request):
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body reads as empty
        body = {}
    return JSONResponse(
        pipedream_client.create_connect_link(str((body or {}).get("app") or ""))
    )


@router.post("/dashboard/connections/pipedream/sync")
def pipedream_sync(request: Request) -> JSONResponse:
    """Explicit resync of connected apps → mcporter servers."""
    if err := _gate(request):
        return err
    return JSONResponse(pipedream_client.sync_mcporter())
