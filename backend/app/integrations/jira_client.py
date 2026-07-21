"""Jira Cloud; the org's issue tracker (read), mirroring asana_client.

Two ways to connect, both self-serve from the Connections tab:
  * OAuth (Atlassian 3LO): "Connect Jira" bounces the owner to the Atlassian
    login/consent screen; the callback stores a refresh token (provider=
    "jira-oauth"). Needs a one-time Atlassian OAuth app (JIRA_CLIENT_ID/SECRET).
    Reads hit https://api.atlassian.com/ex/jira/{cloudid}/rest/api/3 with a
    minted Bearer access token.
  * API token: paste a Jira site URL + email + API token (provider="jira");
    Basic auth against {site}/rest/api/3. Works with NO deploy credentials.
An env fallback (JIRA_SITE/JIRA_EMAIL/JIRA_API_TOKEN) covers single-tenant.

Contract; identical to asana_client / google_client: takes ``org_id``; a
wrong/absent org yields "not connected". Returns ``{"ok": True, ...}`` or
``{"ok": False, "error"}``. NEVER raises (runs at session start, off the live
path). Logs no token and no issue content.
"""
from __future__ import annotations

import base64
import threading
import time
from typing import Any

import httpx

from .. import store
from ..config import settings

_TIMEOUT = 20.0

# Atlassian 3LO OAuth endpoints.
_AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
_OAUTH_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
# offline_access mints a refresh token; read scopes cover the workspace brief.
OAUTH_SCOPES = "read:jira-work read:jira-user offline_access"

# OAuth access-token cache (Atlassian access tokens live ~1h; mint once/hour).
_oauth_lock = threading.Lock()
_OAUTH_CACHE: dict[str, tuple[str, float]] = {}
_OAUTH_EXPIRY_MARGIN_S = 120.0
_OAUTH_DEFAULT_TTL_S = 3300.0

# Workspace-brief cache (fetched at join; same shape as asana_client's).
_BRIEF_TTL_SECONDS = 600.0
_brief_cache: dict[str, tuple[float, str]] = {}
_brief_lock = threading.Lock()

_BRIEF_MAX_ISSUES = 12
_BRIEF_MAX_CHARS = 2400


def oauth_available() -> bool:
    """True when an Atlassian OAuth app is configured; the one-click redirect.
    The paste-a-token path works without it."""
    return bool(settings.jira_client_id and settings.jira_client_secret)


def _basic(email: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def _oauth_access_token(org_id: str, row: dict) -> tuple[str, str]:
    """Mint (or serve cached) a short-lived Bearer token from the org's OAuth
    refresh token. ("", error) on any failure — the caller falls through."""
    now = time.time()
    with _oauth_lock:
        cached = _OAUTH_CACHE.get(org_id)
        if cached and cached[1] > now:
            return cached[0], ""
    if not oauth_available():
        return "", "Jira OAuth app is not configured"
    try:
        resp = httpx.post(_OAUTH_TOKEN_URL, json={
            "grant_type": "refresh_token",
            "client_id": settings.jira_client_id,
            "client_secret": settings.jira_client_secret,
            "refresh_token": row.get("refresh_token", ""),
        }, timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return "", f"jira token request failed ({type(e).__name__})"
    if resp.status_code >= 300:
        return "", f"jira token refresh rejected (HTTP {resp.status_code})"
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return "", "jira token endpoint returned no JSON"
    tok = str(data.get("access_token") or "")
    if not tok:
        return "", "jira token endpoint returned no access_token"
    ttl = min(float(data.get("expires_in") or _OAUTH_DEFAULT_TTL_S), _OAUTH_DEFAULT_TTL_S)
    with _oauth_lock:
        _OAUTH_CACHE[org_id] = (tok, now + ttl - _OAUTH_EXPIRY_MARGIN_S)
    return tok, ""


def _conn(org_id: str) -> tuple[str, str, str]:
    """(rest_base, auth_header, "") for the org, or ("", "", error).

    Precedence: the OAuth grant (provider="jira-oauth": Bearer against
    api.atlassian.com/ex/jira/{cloudid}) → the pasted token (provider="jira": 
    Basic against {site}) → the JIRA_* env fallback."""
    org = (org_id or "").strip()
    try:
        oauth_row = store.get_org_oauth(org, provider="jira-oauth")
    except Exception:  # noqa: BLE001
        oauth_row = None
    if oauth_row and oauth_row.get("refresh_token"):
        cloudid = str(oauth_row.get("scopes") or "")  # cloud id stored in scopes
        tok, _err = _oauth_access_token(org, oauth_row)
        if tok and cloudid:
            return f"https://api.atlassian.com/ex/jira/{cloudid}", f"Bearer {tok}", ""
    try:
        row = store.get_org_oauth(org, provider="jira")
    except Exception:  # noqa: BLE001
        row = None
    if row and row.get("refresh_token"):
        site = str(row.get("scopes") or "").rstrip("/")
        email = str(row.get("email") or "")
        token = str(row.get("refresh_token") or "")
        if site and email and token:
            return site, _basic(email, token), ""
    site = settings.jira_site.strip().rstrip("/")
    email = settings.jira_email.strip()
    token = settings.jira_api_token.strip()
    if site and email and token:
        return site, _basic(email, token), ""
    return "", "", "Jira is not connected for this org"


def connected(org_id: str) -> bool:
    """True when this org can reach Jira (OAuth grant, pasted token, or env)."""
    return bool(_conn(org_id)[0])


def _raw_get(base: str, auth_header: str, path: str,
             params: dict | None = None) -> tuple[Any, str]:
    """GET {base}/rest/api/3{path}. (json, "") or (None, error). Never raises,
    never logs the token or issue content."""
    try:
        resp = httpx.get(
            f"{base}/rest/api/3{path}",
            headers={"Authorization": auth_header, "Accept": "application/json"},
            params=params or {},
            timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"jira request failed ({type(e).__name__})"
    if resp.status_code == 401:
        return None, "Jira rejected the credentials (401)"
    if resp.status_code >= 300:
        return None, f"jira error (HTTP {resp.status_code})"
    try:
        return resp.json(), ""
    except Exception:  # noqa: BLE001
        return None, "jira returned a non-JSON body"


def _get(org_id: str, path: str, params: dict | None = None) -> tuple[Any, str]:
    base, auth_header, err = _conn(org_id)
    if err:
        return None, err
    return _raw_get(base, auth_header, path, params)


def verify_token(site: str, email: str, token: str) -> dict:
    """Live check for the paste-a-token connect flow (no org yet): who the
    credentials authenticate as. {"ok", "email", "site", "name"} or
    {"ok": False, "error"}. Never raises, never returns the token."""
    site = (site or "").strip().rstrip("/")
    email = (email or "").strip()
    token = (token or "").strip()
    if not (site and email and token):
        return {"ok": False, "error": "site, email and API token are all required"}
    if not site.startswith("https://"):
        return {"ok": False, "error": "site must be a full https:// URL"}
    me, err = _raw_get(site, _basic(email, token), "/myself")
    if err:
        return {"ok": False, "error": err}
    return {
        "ok": True,
        "email": str((me or {}).get("emailAddress") or email)[:120],
        "name": str((me or {}).get("displayName") or "")[:120],
        "site": site,
    }


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Atlassian 3LO authorization-code exchange for the OAuth callback:
    {"ok", "refresh_token", "cloudid", "site"} or {"ok": False, "error"}.
    Also resolves the accessible Jira site (cloud id). Never raises/logs."""
    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "code is required"}
    if not oauth_available():
        return {"ok": False, "error": "Jira OAuth app is not configured"}
    try:
        resp = httpx.post(_OAUTH_TOKEN_URL, json={
            "grant_type": "authorization_code",
            "client_id": settings.jira_client_id,
            "client_secret": settings.jira_client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }, timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"jira code exchange failed ({type(e).__name__})"}
    if resp.status_code >= 300:
        return {"ok": False, "error": f"jira code exchange failed (HTTP {resp.status_code})"}
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "jira token endpoint returned no JSON"}
    access = str(data.get("access_token") or "")
    refresh = str(data.get("refresh_token") or "")
    if not (access and refresh):
        return {"ok": False, "error": "jira did not return a refresh token (add offline_access)"}
    # Resolve the accessible Jira site (cloud id); reads target it.
    cloudid, site = "", ""
    try:
        rr = httpx.get(_RESOURCES_URL,
                       headers={"Authorization": f"Bearer {access}",
                                "Accept": "application/json"}, timeout=_TIMEOUT)
        resources = rr.json() if rr.status_code < 300 else []
    except Exception:  # noqa: BLE001
        resources = []
    if resources:
        cloudid = str(resources[0].get("id") or "")
        site = str(resources[0].get("url") or "")
    if not cloudid:
        return {"ok": False, "error": "no accessible Jira site for this account"}
    return {"ok": True, "refresh_token": refresh, "cloudid": cloudid, "site": site}


def _issue_row(issue: dict) -> dict:
    fields = (issue or {}).get("fields") or {}
    assignee = (fields.get("assignee") or {}) or {}
    status = (fields.get("status") or {}) or {}
    return {
        "key": str((issue or {}).get("key") or ""),
        "summary": str(fields.get("summary") or "")[:140],
        "assignee": str(assignee.get("displayName") or "")[:80],
        "due": str(fields.get("duedate") or ""),
        "status": str(status.get("name") or "")[:40],
    }


def list_projects(org_id: str, *, max_results: int = 20) -> dict:
    """{"ok", "projects": [{key, name}]} or {"ok": False, "error"}."""
    data, err = _get(org_id, "/project/search", {"maxResults": max_results})
    if err:
        return {"ok": False, "error": err}
    projects = [
        {"key": str(p.get("key") or ""), "name": str(p.get("name") or "")[:120]}
        for p in (data or {}).get("values", [])
    ]
    return {"ok": True, "projects": projects}


def _build_brief(org_id: str) -> str:
    """A compact, capped snapshot of the org's open Jira issues for the join
    brief, with owners + due dates. PII-safe (no descriptions)."""
    data, err = _get(org_id, "/search", {
        "jql": "statusCategory != Done ORDER BY updated DESC",
        "maxResults": _BRIEF_MAX_ISSUES,
        "fields": "summary,assignee,duedate,status",
    })
    if err:
        return ""
    issues = [_issue_row(i) for i in (data or {}).get("issues", [])]
    if not issues:
        return ""
    lines = ["Open Jira issues:"]
    for it in issues:
        bits = [f"{it['key']}: {it['summary']}"]
        if it["assignee"]:
            bits.append(f"owner: {it['assignee']}")
        if it["due"]:
            bits.append(f"due: {it['due']}")
        if it["status"]:
            bits.append(it["status"])
        lines.append("- " + " · ".join(bits))
    return "\n".join(lines)[:_BRIEF_MAX_CHARS]


def workspace_brief(org_id: str) -> str:
    """TTL-cached join-time snapshot of the org's Jira. "" when not connected
    or empty. Best-effort — never raises onto the join path."""
    org = (org_id or "").strip()
    if not org:
        return ""
    now = time.time()
    with _brief_lock:
        cached = _brief_cache.get(org)
        if cached and cached[0] > now:
            return cached[1]
    try:
        brief = _build_brief(org)
    except Exception:  # noqa: BLE001; the join never fails on a brief
        brief = ""
    with _brief_lock:
        _brief_cache[org] = (now + _BRIEF_TTL_SECONDS, brief)
    return brief


def _reset_brief_cache() -> None:
    with _brief_lock:
        _brief_cache.clear()
    with _oauth_lock:
        _OAUTH_CACHE.clear()
