"""Granola client; pull finished meeting transcripts (post-meeting only).

Granola is a meeting notetaker. Its API returns transcripts of meetings you've
ALREADY recorded with Granola; there is no real-time stream and it cannot join a
call or render an avatar. So it powers the *post-meeting* artifact path (summary +
gap checklist + follow-up email) as a keyless-vendor alternative to Recall; it
does NOT power the live in-call agent.

API (per Granola's OpenAPI):
  GET /v1/notes                       -> { notes: [{id, title, ...}], hasMore, cursor }
  GET /v1/notes/{id}?include=transcript
                                      -> note with .transcript: [
                                           { speaker: {source, name?, diarization_label?},
                                             text, start_time, end_time } ]
Auth: Authorization: Bearer grn_...   (Granola app → Settings → Connectors → API keys)

INTEGRATION SEAM: field names follow the current OpenAPI; if Granola changes the
shape, adjust `_format_transcript`: it's the only vendor-specific parser here.
"""
from __future__ import annotations

import httpx

from ..config import settings


def _headers() -> dict:
    if not settings.granola_api_key:
        raise RuntimeError("GRANOLA_API_KEY is not set.")
    return {"Authorization": f"Bearer {settings.granola_api_key}"}


def list_notes(limit: int = 20) -> list[dict]:
    """List recent Granola notes (only ones with a generated summary + transcript)."""
    resp = httpx.get(
        f"{settings.granola_api_base.rstrip('/')}/v1/notes",
        headers=_headers(),
        params={"limit": limit},
        timeout=30.0,
    )
    resp.raise_for_status()
    notes = resp.json().get("notes", [])
    return [
        {
            "id": n.get("id"),
            "title": n.get("title") or n.get("name") or "(untitled)",
            "created_at": n.get("created_at") or n.get("createdAt") or "",
        }
        for n in notes
    ]


def get_transcript(note_id: str) -> str:
    """Fetch one note's transcript as plain 'Speaker: text' lines (post-meeting)."""
    resp = httpx.get(
        f"{settings.granola_api_base.rstrip('/')}/v1/notes/{note_id}",
        headers=_headers(),
        params={"include": "transcript"},
        timeout=60.0,
    )
    resp.raise_for_status()
    return _format_transcript(resp.json().get("transcript") or [])


def _format_transcript(segments: list[dict]) -> str:
    lines: list[str] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker = seg.get("speaker") or {}
        who = (
            speaker.get("name")
            or speaker.get("diarization_label")
            or speaker.get("source")
            or "Unknown"
        )
        lines.append(f"{who}: {text}")
    return "\n".join(lines)
