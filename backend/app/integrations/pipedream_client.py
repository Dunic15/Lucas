"""Pipedream Connect client — the dashboard's "connect any app" door for the
OpenClaw action plane.

Pipedream holds the OAuth grants (~2,800 apps) under one Connect project; this
module mints connect links so the dashboard can add app connections, lists
what's connected, and syncs each connected app into the local mcporter config
so the OpenClaw agent sees it as an MCP toolset (server ``pd-<app>`` →
``~/.mcporter/pipedream-mcp.sh``, which fetches a fresh Pipedream token per
session). Dev seam: the sync writes a LOCAL file next to the local gateway,
so it is gated on ``openclaw_executor`` alongside the route that consumes it.

Never on the live path; every call soft-fails to ``{"ok": False, "error"}``.
The client secret comes from settings/env only — never logged, never in git.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

_API = "https://api.pipedream.com/v1"
_TIMEOUT = 15.0

# Access-token cache: Pipedream Connect tokens live ~1h; mint once per ~50min.
_token_lock = threading.Lock()
_token_cache: tuple[str, float] | None = None  # (token, expires_at)
_TOKEN_TTL_S = 3000.0

# mcporter server names derive from app slugs: pd-google_calendar → the agent
# calls `mcporter call pd-google_calendar.<tool>`. Non-pd entries are preserved.
_SERVER_PREFIX = "pd-"


def enabled() -> bool:
    """True when the Connect project credentials are configured."""
    return bool(
        settings.pipedream_project_id
        and settings.pipedream_client_id
        and settings.pipedream_client_secret
    )


def _access_token() -> tuple[str, str]:
    """(token, "") via client-credentials grant, cached; ("", error) on failure."""
    global _token_cache
    with _token_lock:
        if _token_cache and _token_cache[1] > time.time():
            return _token_cache[0], ""
    try:
        r = httpx.post(
            f"{_API}/oauth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": settings.pipedream_client_id,
                "client_secret": settings.pipedream_client_secret,
            },
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        tok = str(r.json().get("access_token") or "")
    except Exception as e:  # noqa: BLE001 — vendor down reads as not connected
        return "", f"pipedream token failed ({type(e).__name__})"
    if not tok:
        return "", "pipedream token missing in response"
    with _token_lock:
        _token_cache = (tok, time.time() + _TOKEN_TTL_S)
    return tok, ""


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "x-pd-environment": settings.pipedream_environment,
    }


def search_apps(query: str, limit: int = 20) -> dict:
    """Search Pipedream's app catalog: {"ok", "apps": [{slug, name, ...}]}."""
    if not enabled():
        return {"ok": False, "error": "pipedream not configured"}
    token, err = _access_token()
    if err:
        return {"ok": False, "error": err}
    try:
        r = httpx.get(
            f"{_API}/apps",
            params={"q": (query or "").strip(), "limit": max(1, min(limit, 50))},
            headers=_headers(token),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json().get("data") or []
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"app search failed ({type(e).__name__})"}
    apps = [
        {
            "slug": a.get("name_slug") or "",
            "name": a.get("name") or "",
            "description": (a.get("description") or "")[:200],
            "auth_type": a.get("auth_type") or "",
        }
        for a in rows
        if isinstance(a, dict) and a.get("name_slug")
    ]
    return {"ok": True, "apps": apps}


def list_accounts() -> dict:
    """Connected accounts for the configured external user:
    {"ok", "accounts": [{app, name, healthy}]}."""
    if not enabled():
        return {"ok": False, "error": "pipedream not configured"}
    token, err = _access_token()
    if err:
        return {"ok": False, "error": err}
    try:
        r = httpx.get(
            f"{_API}/connect/{settings.pipedream_project_id}/accounts",
            params={"external_user_id": settings.pipedream_external_user_id},
            headers=_headers(token),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json().get("data") or []
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"account list failed ({type(e).__name__})"}
    accounts = [
        {
            "app": (a.get("app") or {}).get("name_slug") or "",
            "app_name": (a.get("app") or {}).get("name") or "",
            "name": a.get("name") or "",
            "healthy": bool(a.get("healthy")),
        }
        for a in rows
        if isinstance(a, dict)
    ]
    return {"ok": True, "accounts": accounts}


def create_connect_link(app_slug: str) -> dict:
    """Mint a Connect Link for one app: {"ok", "url", "expires_at"}. The user
    opens the URL, authorizes, and the account lands under the configured
    external user in the configured environment."""
    if not enabled():
        return {"ok": False, "error": "pipedream not configured"}
    slug = (app_slug or "").strip()
    if not slug:
        return {"ok": False, "error": "missing app slug"}
    token, err = _access_token()
    if err:
        return {"ok": False, "error": err}
    try:
        r = httpx.post(
            f"{_API}/connect/{settings.pipedream_project_id}/tokens",
            json={"external_user_id": settings.pipedream_external_user_id},
            headers=_headers(token),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"connect link failed ({type(e).__name__})"}
    url = str(d.get("connect_link_url") or "")
    if not url:
        return {"ok": False, "error": "no connect link in response"}
    return {
        "ok": True,
        "url": f"{url}&app={slug}",
        "expires_at": d.get("expires_at") or "",
    }


def sync_mcporter() -> dict:
    """Regenerate the ``pd-*`` mcporter servers from the connected accounts, so
    every dashboard-connected app becomes an OpenClaw toolset. Preserves any
    non-``pd-`` entries. Local-gateway seam → requires ``openclaw_executor``.

    Returns {"ok", "servers": [names]} or a soft error."""
    if not settings.openclaw_executor:
        return {"ok": False, "error": "openclaw_executor off"}
    listed = list_accounts()
    if not listed.get("ok"):
        return listed
    wrapper = str(Path(settings.pipedream_mcp_wrapper).expanduser())
    cfg_path = Path(settings.mcporter_config_path).expanduser()
    try:
        existing = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    except ValueError:
        existing = {}
    servers = {
        name: entry
        for name, entry in (existing.get("mcpServers") or {}).items()
        if not name.startswith(_SERVER_PREFIX)
    }
    slugs = sorted(
        {a["app"] for a in listed["accounts"] if a.get("app") and a.get("healthy")}
    )
    for slug in slugs:
        servers[f"{_SERVER_PREFIX}{slug}"] = {
            "command": "bash",
            "args": [wrapper, settings.pipedream_external_user_id, slug],
            "description": f"{slug} via Pipedream Connect",
        }
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps({**existing, "mcpServers": servers}, indent=2) + "\n"
        )
    except OSError as e:
        return {"ok": False, "error": f"mcporter config write failed ({type(e).__name__})"}
    return {"ok": True, "servers": sorted(servers)}


def reset_token_cache_for_tests() -> None:
    global _token_cache
    with _token_lock:
        _token_cache = None
