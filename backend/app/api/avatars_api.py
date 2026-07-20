"""Avatar roster + per-avatar brain-mode routes — extracted from main.py."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import auth, avatars, store, gemini_ears

router = APIRouter()


@router.get("/avatars")
def list_avatars(request: Request) -> dict:
    """List installed avatars (one folder each under avatars/). A logged-in
    user sees only their org's granted avatars; the anonymous/demo caller sees
    ALL — the key-free demo picker is unchanged."""
    user = auth.current_user(request)
    roster = (
        avatars.list_for_org(user["org_id"]) if user else avatars.list_ids()
    )
    out = []
    for aid in roster:
        a = avatars.load(aid)
        # Per-avatar brain choice for the dashboard toggle: the stored choice,
        # else derived from the effective mode (global default).
        _explicit = store.get_avatar_brain_mode(aid)
        _brain = _explicit or (
            "gemini"
            if gemini_ears.mode_for_avatar(aid) in ("reply", "on")
            else "cerebras"
        )
        out.append({"id": a.id, "name": a.name, "role": a.role,
                    "wake_words": a.wake_words,
                    "brain": _brain, "brain_explicit": _explicit is not None})
    return {"avatars": out}


class BrainModeRequest(BaseModel):
    brain: str  # "gemini" | "cerebras"


@router.post("/avatars/{avatar_id}/brain-mode")
def set_avatar_brain(
    avatar_id: str, req: BrainModeRequest, request: Request
) -> JSONResponse:
    """Owner sets an avatar's brain from the dashboard: "gemini" (tutto-Gemini
    via the relay) or "cerebras" (the normal Deepgram + grounded brain). Takes
    effect on the avatar's NEXT meeting — no redeploy. Not anonymous: a logged-in
    owner (or the machine bearer) only, so the demo can't flip prod behavior."""
    if err := auth.gate(request):
        return err
    aid = (avatar_id or "").strip()
    if aid not in set(avatars.list_ids()):
        return JSONResponse({"error": "unknown avatar_id"}, status_code=404)
    choice = (req.brain or "").strip().lower()
    if not store.set_avatar_brain_mode(aid, choice):
        return JSONResponse(
            {"error": "brain must be 'gemini' or 'cerebras'"}, status_code=400
        )
    return JSONResponse({"ok": True, "avatar_id": aid, "brain": choice})
