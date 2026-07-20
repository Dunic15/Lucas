"""Granola note/transcript passthrough (read-only; needs GRANOLA_API_KEY).
Extracted from main.py."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.integrations import granola_client

router = APIRouter()


@router.get("/granola/notes")
def granola_notes(limit: int = 20) -> JSONResponse:
    """List recent Granola notes to pick from (needs GRANOLA_API_KEY)."""
    if not settings.granola_api_key:
        return JSONResponse({"error": "GRANOLA_API_KEY not set"}, status_code=400)
    return JSONResponse({"notes": granola_client.list_notes(limit)})


@router.get("/granola/transcript")
def granola_transcript(note_id: str) -> JSONResponse:
    """Fetch one Granola note's transcript as 'Speaker: text' lines."""
    if not settings.granola_api_key:
        return JSONResponse({"error": "GRANOLA_API_KEY not set"}, status_code=400)
    return JSONResponse({"note_id": note_id,
                         "transcript": granola_client.get_transcript(note_id)})
