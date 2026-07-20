"""Static pages + per-avatar model/portrait assets — extracted from main.py.
Pure file-serving with no coupling to the live-meeting path. FRONTEND_DIR /
REPO_ROOT_DIR are derived from core.config.REPO_ROOT (importing them from main
would be circular)."""
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

from app.core.config import REPO_ROOT, settings
from app import avatars

router = APIRouter()
FRONTEND_DIR = REPO_ROOT / "frontend"
REPO_ROOT_DIR = REPO_ROOT


@router.get("/live")
def live_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "live.html")


@router.get("/join")
def join_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "join.html")


@router.get("/login")
def login_page() -> FileResponse:
    """Dashboard sign-in (Google). With no Google client configured the page
    offers the open demo-mode dashboard instead — key-free demo preserved."""
    return FileResponse(FRONTEND_DIR / "login.html")


@router.get("/privacy")
def privacy_page() -> FileResponse:
    """Public privacy policy — required by the Google OAuth consent screen
    (and linked from the marketing site). Static, no data, no auth."""
    return FileResponse(FRONTEND_DIR / "privacy.html")


@router.get("/terms")
def terms_page() -> FileResponse:
    """Public terms of service — companion to /privacy for the consent
    screen and checkout. Static, no data, no auth."""
    return FileResponse(FRONTEND_DIR / "terms.html")


@router.get("/talk")
def talk_page() -> FileResponse:
    """Open-source avatar page (TalkingHead + our TTS) — the Anam replacement.
    Recall will render this instead of avatar.html once it's proven out.

    no-store: the page's JS changes often (framing, barge-in, streaming) — without
    this browsers serve a stale cached copy and users see old behaviour."""
    return FileResponse(
        FRONTEND_DIR / "talk.html", headers={"Cache-Control": "no-store"}
    )


@router.get("/photoreal")
def photoreal_page() -> FileResponse:
    """Photoreal avatar page (Stage 2): GPU-streamed MuseTalk face. Same speak
    contract as /talk; flip meetings onto it with AVATAR_PAGE=photoreal once
    the GPU box is live (see gpu/README.md)."""
    return FileResponse(FRONTEND_DIR / "photoreal.html")


@router.get("/photoreal/config")
def photoreal_config(avatar_id: str = "") -> JSONResponse:
    """GPU endpoint plus identity-safe readiness for the requested avatar."""
    try:
        avatar = avatars.load(avatar_id or settings.default_avatar_id)
        renderer = avatar.renderer_readiness
    except (FileNotFoundError, ValueError):
        return JSONResponse(
            {"error": "face_unavailable", "avatar_id": avatar_id},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {
            "stream_url": settings.gpu_stream_url,
            "avatar_id": avatar.id,
            "face_ready": renderer["photoreal"]["ready"],
            "fallback": renderer["fallback"],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/laura-reference.jpg")
def photoreal_reference(avatar_id: str = "") -> Response:
    """Return only the requested avatar's portrait; never another identity."""
    assets = REPO_ROOT_DIR / "gpu" / "assets"
    if not avatar_id:
        # Legacy/manual preview without an identity remains Laura-only.
        path = assets / "reference.jpg"
    elif not re.fullmatch(r"[a-z0-9_-]{1,64}", avatar_id.lower()):
        return JSONResponse({"error": "face_unavailable"}, status_code=404)
    else:
        try:
            avatar = avatars.load(avatar_id.lower())
            name = avatar.photoreal_reference or f"reference-{avatar.id}.jpg"
            path = assets / name if Path(name).name == name else assets / "__missing__"
        except FileNotFoundError:
            path = assets / "__missing__"
    if not path.is_file():
        return JSONResponse(
            {"error": "face_unavailable", "avatar_id": avatar_id}, status_code=404
        )
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.api_route("/{avatar_id}.glb", methods=["GET", "HEAD"])
def talk_avatar_model(avatar_id: str) -> Response:
    """Per-avatar 3D model for /talk (laura.glb, cedric.glb, …), served
    same-origin on purpose: Ready Player Me's CDN shutdown (Jan 2026) killed our
    previous third-party model URL, so the models (TalkingHead-repo samples) are
    vendored into frontend/. /talk HEAD-probes /{avatar_id}.glb and may fall
    back only to that same avatar's configured renderer; a missing model 404s
    explicitly and never borrows another identity.
    HEAD must be explicit — FastAPI's @router.get alone 405s it, which would have
    silently defeated the probe (curl -I caught this; FileResponse handles HEAD
    natively). Whitelisted to simple ids resolving to real files — never a
    path traversal."""
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", avatar_id):
        return JSONResponse({"error": "unknown model"}, status_code=404)
    model_path = FRONTEND_DIR / f"{avatar_id}.glb"
    if not model_path.is_file():
        return JSONResponse({"error": "unknown model"}, status_code=404)
    return FileResponse(
        model_path,
        media_type="model/gltf-binary",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/avatar")
def avatar_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "avatar.html")
