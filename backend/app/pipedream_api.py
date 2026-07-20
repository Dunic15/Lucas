"""Pipedream Connect — dashboard HTTP surface for the alternative connections
tab (EXPERIMENTAL / test-only).

Every route 404s cleanly when the feature flag is off (project + client creds
unset), so the key-free demo and existing deployments never see a new surface
by accident. Reads are login-gated; the connect redirect is a GET navigation
(cookie-authenticated, like /oauth/google/connect); the test action-run is
login + same-origin gated. The end user is the caller's org_id — connections
are per-org. No credentials/tokens are ever returned to the browser.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import auth, pipedream_client
from .config import settings

router = APIRouter(tags=["pipedream"])

_NO_STORE = {"Cache-Control": "no-store"}

# The apps offered in the alternative tab. Slug = Pipedream app slug (used to
# scope the Connect Link and match connected accounts). Mirrors the native
# cards (Google/Slack/Asana/Jira) plus a few Pipedream unlocks.
_CATALOG = [
    {"slug": "slack", "name": "Slack"},
    {"slug": "gmail", "name": "Gmail"},
    {"slug": "google_calendar", "name": "Google Calendar"},
    {"slug": "google_sheets", "name": "Google Sheets"},
    {"slug": "google_drive", "name": "Google Drive"},
    {"slug": "asana", "name": "Asana"},
    {"slug": "jira", "name": "Jira"},
    {"slug": "notion", "name": "Notion"},
    {"slug": "hubspot", "name": "HubSpot"},
    {"slug": "github", "name": "GitHub"},
    {"slug": "linear", "name": "Linear"},
]
_ALLOWED_SLUGS = {a["slug"] for a in _CATALOG}


def _disabled() -> JSONResponse:
    return JSONResponse(
        {"error": "pipedream_disabled"}, status_code=404, headers=_NO_STORE,
    )


def _dash_org(request: Request) -> tuple[JSONResponse | None, str]:
    """Login gate (mirrors the knowledge dashboard twin). Cookie user required;
    same-origin required for state-changing methods."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err, ""
        return JSONResponse({"error": "login required"}, status_code=401), ""
    if request.method != "GET" and not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403), ""
    return None, user["org_id"]


@router.get("/dashboard/pipedream/accounts")
async def pipedream_accounts(request: Request) -> JSONResponse:
    """The catalog + which apps this org has connected through Pipedream. Soft
    on API failure: still returns the catalog so the tab renders (degraded)."""
    if not pipedream_client.enabled():
        return _disabled()
    err, org = _dash_org(request)
    if err is not None:
        return err
    degraded = ""
    connected: dict[str, dict] = {}
    try:
        for acct in pipedream_client.list_accounts(org):
            slug = str(acct.get("app") or "")
            if slug:
                connected[slug] = acct
    except pipedream_client.PipedreamError as exc:
        degraded = type(exc).__name__
    apps = [
        {**a,
         "connected": a["slug"] in connected,
         "account": connected.get(a["slug"]) or None}
        for a in _CATALOG
    ]
    return JSONResponse(
        {"ok": True, "environment": pipedream_client.environment(),
         "apps": apps, "degraded": degraded},
        headers=_NO_STORE,
    )


@router.get("/dashboard/pipedream/connect", response_model=None)
async def pipedream_connect(request: Request, app: str = "") -> RedirectResponse | JSONResponse:
    """Start a Pipedream Connect flow for one app: mint a per-org connect token
    with redirect-back URIs, then 302 the browser to the hosted Connect Link.
    GET navigation (cookie-authenticated), like /oauth/google/connect."""
    if not pipedream_client.enabled():
        return _disabled()
    err, org = _dash_org(request)
    if err is not None:
        return err
    slug = (app or "").strip().lower()
    if slug not in _ALLOWED_SLUGS:
        return JSONResponse({"error": "unknown app"}, status_code=400,
                            headers=_NO_STORE)
    base = settings.public_base_url.rstrip("/")
    try:
        minted = pipedream_client.create_connect_token(
            org, app=slug,
            success_redirect_uri=f"{base}/dashboard?pd=connected&app={slug}",
            error_redirect_uri=f"{base}/dashboard?pd=error&app={slug}",
        )
    except pipedream_client.PipedreamError:
        return RedirectResponse(f"{base}/dashboard?pd=error&app={slug}",
                                status_code=303)
    connect_url = minted.get("connect_url") or ""
    if not connect_url:
        return RedirectResponse(f"{base}/dashboard?pd=error&app={slug}",
                                status_code=303)
    return RedirectResponse(connect_url, status_code=303)


@router.post("/dashboard/pipedream/run")
async def pipedream_run(request: Request) -> JSONResponse:
    """Run a pre-built Pipedream action on the org's connected account — the
    'pre-configured actions' surface, for testing. Login + same-origin gated."""
    if not pipedream_client.enabled():
        return _disabled()
    err, org = _dash_org(request)
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    action_id = str((body or {}).get("action_id") or "").strip()
    props = (body or {}).get("configured_props")
    if not action_id or not isinstance(props, dict):
        return JSONResponse(
            {"error": "action_id and configured_props required"},
            status_code=400, headers=_NO_STORE,
        )
    try:
        result = pipedream_client.run_action(org, action_id, props)
    except pipedream_client.PipedreamError as exc:
        return JSONResponse({"error": "run_failed", "detail": type(exc).__name__},
                            status_code=502, headers=_NO_STORE)
    # Distilled: exports + observations only (never raw credentials).
    return JSONResponse(
        {"ok": True, "exports": result.get("exports"),
         "os": result.get("os"), "ret": result.get("ret")},
        headers=_NO_STORE,
    )
