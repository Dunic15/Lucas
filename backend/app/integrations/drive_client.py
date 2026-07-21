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
from urllib.parse import urlencode

import httpx

from . import gmail_watcher

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


# ── Pipedream Connect-Proxy reader (managed-OAuth Drive) ────────────────────
# The cutover moves Drive off Laura's native drive.readonly token onto the org's
# Pipedream-connected google_drive account. Same two calls (list folder, export/
# download a file), but the credential is injected server-side by the proxy — no
# raw token is ever held here. Falls back to the native token when the org has
# not connected Drive in Pipedream (see folder_brief).

def _pd_drive_account(org_id: str) -> str:
    """The org's connected google_drive account id in Pipedream, or "" — cheap
    cached availability probe first, so a non-Pipedream org pays nothing."""
    from .. import pipedream_client, pipedream_executor  # lazy: load-order safe

    if not (org_id and pipedream_executor.app_connected(org_id, "google_drive")):
        return ""
    try:
        accts = pipedream_client.list_accounts(org_id, app="google_drive")
    except pipedream_client.PipedreamError:
        return ""
    acct = next((a for a in accts if a.get("id")), None)
    return str(acct["id"]) if acct else ""


def _list_folder_pd(org_id: str, account_id: str, folder_id: str) -> list[dict]:
    from .. import pipedream_client

    q = urlencode({
        "q": f"'{folder_id}' in parents and trashed=false",
        "fields": "files(id,name,mimeType,modifiedTime)",
        "orderBy": "modifiedTime desc",
        "pageSize": MAX_FILES,
    })
    resp = pipedream_client.proxy_request(
        org_id, account_id, "GET", f"{DRIVE_API}/files?{q}")
    if not resp.get("ok"):
        return []
    return (resp.get("json") or {}).get("files") or []


def _file_text_pd(org_id: str, account_id: str, file: dict) -> str:
    from .. import pipedream_client

    mime = str(file.get("mimeType") or "")
    fid = file.get("id") or ""
    if mime == _EXPORTABLE:
        url = f"{DRIVE_API}/files/{fid}/export?{urlencode({'mimeType': 'text/plain'})}"
    elif mime.startswith(_PLAIN_PREFIXES) or mime in _PLAIN_TYPES:
        url = f"{DRIVE_API}/files/{fid}?{urlencode({'alt': 'media'})}"
    else:
        return ""
    resp = pipedream_client.proxy_request(org_id, account_id, "GET", url)
    if not resp.get("ok"):
        return ""
    # Export/download bodies are non-JSON → proxy_request returns them as text.
    return str(resp.get("text") or "")


def folder_brief(folder_id: str, org_id: str = "") -> str:
    """Markdown brief of a Drive folder's docs; "" when unset or unavailable.

    Reads through the org's Pipedream-connected google_drive account when it has
    one (the cutover path); otherwise the native drive.readonly token. Sync
    (network) — call via run_in_threadpool at session start only.
    """
    folder_id = (folder_id or "").strip()
    if not folder_id:
        return ""
    now = time.time()
    # Cache per (org, folder): the Pipedream and native paths can differ.
    ckey = f"{org_id}::{folder_id}"
    cached = _cache.get(ckey)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    text = ""
    try:
        pd_account = _pd_drive_account(org_id)
        parts: list[str] = []
        if pd_account:
            for f in _list_folder_pd(org_id, pd_account, folder_id):
                try:
                    body = _file_text_pd(org_id, pd_account, f).strip()
                except Exception:  # noqa: BLE001 — one bad file never kills the brief
                    body = ""
                if body:
                    parts.append(f"## {f.get('name', 'untitled')}\n\n{body}")
        else:
            token = _access_token()
            if token:
                for f in _list_folder(token, folder_id):
                    try:
                        body = _file_text(token, f).strip()
                    except Exception:  # noqa: BLE001 — one bad file never kills it
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
    _cache[ckey] = (now, text)
    return text
