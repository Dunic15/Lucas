"""Google Drive folder → pre-meeting brief (the Drive connector).

An avatar with `drive_folder_id` in its avatar.yaml walks into every meeting
knowing what's in that shared Drive folder: at session START the folder's docs
are pulled and injected into the same memory channel as the cross-meeting
brief. Reuses the Google OAuth the calendar/Gmail machinery already holds —
the `drive.readonly` scope is part of /oauth/google/connect; reconnect once
after upgrading so the stored refresh token carries it.

Design rules (same discipline as every connector here):
- NEVER on the live path: fetched once at session start, in a threadpool.
- Best-effort: any failure -> "" and the avatar joins without the folder.
- Bounded: the brief is capped so a huge folder can't bloat live prompts
  (prompt tokens are latency on the fast model).
- Cached per folder for a few minutes: calendar/Gmail/manual starts can race;
  one fetch serves them all.
"""
from __future__ import annotations

import time

import httpx

from .. import gmail_watcher

DRIVE_API = "https://www.googleapis.com/drive/v3"

# Live prompts pay per token: cap the folder brief well below the orchestrator
# brief cap. ~8KB ≈ a few pages — enough for status docs, not a wiki dump.
MAX_BRIEF_BYTES = 8 * 1024
MAX_FILES = 12
_CACHE_TTL_SECONDS = 240.0

_cache: dict[str, tuple[float, str]] = {}
# Bot creation happens BEFORE this fetch, so a slow Drive never delays the
# join — only the start-API response. Keep that bound tight.
_client = httpx.Client(timeout=8.0)

# Google-native docs export as plain text; plain/markdown files download as-is.
# Sheets/slides/binaries are skipped in v1 (an export would dwarf the cap).
_EXPORTABLE = "application/vnd.google-apps.document"
_PLAIN_PREFIXES = ("text/",)
_PLAIN_TYPES = {"application/json"}


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _access_token() -> str:
    rt = gmail_watcher.refresh_token()
    return gmail_watcher.access_token(rt) if rt else ""


def _list_folder(token: str, folder_id: str) -> list[dict]:
    r = _client.get(
        f"{DRIVE_API}/files",
        params={
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "files(id,name,mimeType,modifiedTime)",
            "orderBy": "modifiedTime desc",
            "pageSize": MAX_FILES,
        },
        headers=_headers(token),
    )
    r.raise_for_status()
    return r.json().get("files", []) or []


def _file_text(token: str, file: dict) -> str:
    mime = str(file.get("mimeType") or "")
    fid = file.get("id") or ""
    if mime == _EXPORTABLE:
        r = _client.get(
            f"{DRIVE_API}/files/{fid}/export",
            params={"mimeType": "text/plain"},
            headers=_headers(token),
        )
    elif mime.startswith(_PLAIN_PREFIXES) or mime in _PLAIN_TYPES:
        r = _client.get(
            f"{DRIVE_API}/files/{fid}",
            params={"alt": "media"},
            headers=_headers(token),
        )
    else:
        return ""
    r.raise_for_status()
    return r.text or ""


def folder_brief(folder_id: str) -> str:
    """Markdown brief of a Drive folder's docs; "" when unset or unavailable.

    Sync (network) — call via run_in_threadpool at session start only.
    """
    folder_id = (folder_id or "").strip()
    if not folder_id:
        return ""
    now = time.time()
    cached = _cache.get(folder_id)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    text = ""
    try:
        token = _access_token()
        if token:
            parts: list[str] = []
            for f in _list_folder(token, folder_id):
                try:
                    body = _file_text(token, f).strip()
                except Exception:  # noqa: BLE001 — one bad file never kills the brief
                    body = ""
                if body:
                    parts.append(f"## {f.get('name', 'untitled')}\n\n{body}")
            text = "\n\n".join(parts)
            if len(text.encode()) > MAX_BRIEF_BYTES:
                text = (
                    text.encode()[:MAX_BRIEF_BYTES].decode("utf-8", "ignore")
                    + "\n… (folder brief truncated)"
                )
    except Exception as e:  # noqa: BLE001 — best-effort: join without the folder
        # Folder id + error class only — never file contents in logs.
        print(f"[drive] folder brief unavailable ({type(e).__name__})", flush=True)
        text = ""
    _cache[folder_id] = (now, text)
    return text
