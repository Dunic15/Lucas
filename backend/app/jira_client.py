"""Jira Cloud — the org's issue tracker (read), mirroring asana_client.

Given an org that connected Jira, read the workspace (projects, open/assigned
issues) into a compact meeting brief so the PM avatar can answer from it — the
same shape as the Asana connector.

Auth (self-serve, NO deploy credentials): a Jira Cloud **API token** with the
account **email** and the **site URL** (e.g. https://acme.atlassian.net). Jira
Cloud authenticates the REST API with HTTP Basic `email:token`. Stored encrypted
per org (``store.set_org_oauth(org, token, provider="jira", email=<email>,
scopes=<site_url>)``); an env fallback (JIRA_SITE / JIRA_EMAIL / JIRA_API_TOKEN)
covers single-tenant. OAuth-redirect (Atlassian 3LO) is a future upgrade behind
JIRA_CLIENT_ID/SECRET — see oauth_available(); the token path needs none of it.

Contract — identical to asana_client / google_client:
- Takes ``org_id``; a wrong/absent org yields "not connected", never a call
  from the wrong account. Returns ``{"ok": True, ...}`` or ``{"ok": False,
  "error"}``. NEVER raises (runs at session start, off the live path). Logs no
  token and no issue content.
"""
from __future__ import annotations

import base64
import threading
import time
from typing import Any

import httpx

from . import store
from .config import settings

_TIMEOUT = 20.0

# Workspace-brief cache (fetched at join; same shape as asana_client's).
_BRIEF_TTL_SECONDS = 600.0
_brief_cache: dict[str, tuple[float, str]] = {}
_brief_lock = threading.Lock()

# Caps that keep the join-time brief cheap — it rides the live prompt.
_BRIEF_MAX_PROJECTS = 8
_BRIEF_MAX_ISSUES = 12
_BRIEF_MAX_CHARS = 2400


def oauth_available() -> bool:
    """True when a Jira (Atlassian) OAuth app is configured — the future
    one-click redirect. The token path below works without it."""
    return bool(settings.jira_client_id and settings.jira_client_secret)


def _creds(org_id: str) -> tuple[str, str, str, str]:
    """(site_url, email, api_token, "") for the org, or ("","","", error).

    Precedence: the per-org stored token (provider="jira") → the JIRA_* env
    fallback. `site_url` is normalized to no trailing slash."""
    org = (org_id or "").strip()
    try:
        row = store.get_org_oauth(org, provider="jira")
    except Exception:  # noqa: BLE001 — a store hiccup reads as not connected
        row = None
    if row and row.get("refresh_token"):
        site = str(row.get("scopes") or "").rstrip("/")
        email = str(row.get("email") or "")
        token = str(row.get("refresh_token") or "")
        if site and email and token:
            return site, email, token, ""
    site = settings.jira_site.strip().rstrip("/")
    email = settings.jira_email.strip()
    token = settings.jira_api_token.strip()
    if site and email and token:
        return site, email, token, ""
    return "", "", "", "Jira is not connected for this org"


def connected(org_id: str) -> bool:
    """True when this org can reach Jira (per-org token or env fallback)."""
    return bool(_creds(org_id)[0])


def _auth_header(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _get(site: str, email: str, token: str, path: str,
         params: dict | None = None) -> tuple[Any, str]:
    """GET {site}/rest/api/3{path}. (json, "") or (None, error). Never raises,
    never logs the token or issue content."""
    try:
        resp = httpx.get(
            f"{site}/rest/api/3{path}",
            headers={"Authorization": _auth_header(email, token),
                     "Accept": "application/json"},
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


def verify_token(site: str, email: str, token: str) -> dict:
    """Live check for the connect flow: who the credentials authenticate as.
    {"ok", "email", "site", "name"} or {"ok": False, "error"}. Never raises,
    never returns the token."""
    site = (site or "").strip().rstrip("/")
    email = (email or "").strip()
    token = (token or "").strip()
    if not (site and email and token):
        return {"ok": False, "error": "site, email and API token are all required"}
    if not site.startswith("https://"):
        return {"ok": False, "error": "site must be a full https:// URL"}
    me, err = _get(site, email, token, "/myself")
    if err:
        return {"ok": False, "error": err}
    return {
        "ok": True,
        "email": str((me or {}).get("emailAddress") or email)[:120],
        "name": str((me or {}).get("displayName") or "")[:120],
        "site": site,
    }


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
    site, email, token, err = _creds(org_id)
    if err:
        return {"ok": False, "error": err}
    data, err = _get(site, email, token, "/project/search",
                     {"maxResults": max_results})
    if err:
        return {"ok": False, "error": err}
    projects = [
        {"key": str(p.get("key") or ""), "name": str(p.get("name") or "")[:120]}
        for p in (data or {}).get("values", [])
    ]
    return {"ok": True, "projects": projects}


def _search_issues(site: str, email: str, token: str, jql: str,
                   max_results: int) -> tuple[list[dict], str]:
    data, err = _get(site, email, token, "/search", {
        "jql": jql,
        "maxResults": max_results,
        "fields": "summary,assignee,duedate,status",
    })
    if err:
        return [], err
    return [_issue_row(i) for i in (data or {}).get("issues", [])], ""


def _build_brief(org_id: str) -> str:
    """A compact, capped snapshot of the org's Jira for the join brief: open
    issues (newest), with owners + due dates. PII-safe (no descriptions)."""
    site, email, token, err = _creds(org_id)
    if err:
        return ""
    issues, err = _search_issues(
        site, email, token,
        "statusCategory != Done ORDER BY updated DESC",
        _BRIEF_MAX_ISSUES,
    )
    if err or not issues:
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
    except Exception:  # noqa: BLE001 — the join never fails on a brief
        brief = ""
    with _brief_lock:
        _brief_cache[org] = (now + _BRIEF_TTL_SECONDS, brief)
    return brief


def _reset_brief_cache() -> None:
    with _brief_lock:
        _brief_cache.clear()
