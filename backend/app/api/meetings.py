"""Meeting reads: /ledger, /meetings, /meetings/list — extracted from main.py."""
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from app import auth, cedric, ledger, store
from app.config import settings, REPO_ROOT

router = APIRouter()
FRONTEND_DIR = REPO_ROOT / "frontend"


@router.get("/ledger")
def ledger_view(meeting_url: str, request: Request) -> JSONResponse:
    """Cross-meeting memory for a meeting link: every ledger item plus the
    carryover brief the avatar gets injected at the next session."""
    # Machine/service seam: a PER-ORG bearer reads ITS org's memory; the
    # global bearer (and the key-free open demo) keeps the Demo org, exactly
    # as today. Sync handler → FastAPI already runs this off the event loop.
    machine_org = cedric.resolve_machine_org(request)
    if machine_org is None:
        if err := cedric.auth_error(request):  # CEDRIC
            return err
    org = machine_org or settings.demo_org_id
    key = ledger.meeting_key(meeting_url)
    return JSONResponse(
        {
            "meeting_key": key,
            "brief": ledger.carryover_brief(meeting_url, org_id=org),
            "items": ledger.items(key, org_id=org),
        }
    )


@router.get("/meetings")
def meetings_page() -> FileResponse:
    """Archive UI: every finished meeting's artifact, transcript included."""
    return FileResponse(FRONTEND_DIR / "meetings.html")


@router.get("/meetings/list")
def meetings_list(request: Request) -> JSONResponse:
    """All saved artifacts, newest first, for the /meetings page. Transcripts
    are PII: gated to a logged-in owner (their own org) or the machine bearer —
    never served to the anonymous internet — and never logged. Same guard as
    /dashboard/summary; the HTML shell (/meetings) stays open like /dashboard."""
    user = auth.current_user(request)
    machine_org = None
    if user is None:
        machine_org = cedric.resolve_machine_org(request)
        if machine_org is None:
            if err := auth.gate(request):
                return err
    artifact_scope = None
    if store.durable_artifacts_enabled():
        artifact_scope = (
            machine_org
            or (str(user["org_id"]) if user is not None else None)
        )
        # A deployment-level service bearer is never permission to enumerate
        # every tenant. In production it retains only the Demo workspace.
        if artifact_scope is None:
            artifact_scope = settings.demo_org_id
    # Key-free SQLite intentionally keeps its historical global read followed
    # by the legacy/unowned visibility filter below.
    artifacts = store.list_artifacts(artifact_scope)
    if machine_org is not None:
        artifacts = [
            a
            for a in artifacts
            if str((a.get("artifact") or {}).get("org_id") or "") == machine_org
        ]
    elif user is not None:
        # Cookie login: scope to the caller's org. Unowned/legacy artifacts
        # (empty org_id) stay visible, mirroring the /sessions/*/redeliver
        # rule; DEMO-org artifacts do not — self-serve product decision
        # (2026-07-13): the anonymous showroom's transcripts never appear in a
        # real signup's archive.
        org = str(user["org_id"])
        artifacts = [
            a
            for a in artifacts
            if (art_org := str((a.get("artifact") or {}).get("org_id") or ""))
            in ("", org)
        ]
    return JSONResponse({"meetings": artifacts})
