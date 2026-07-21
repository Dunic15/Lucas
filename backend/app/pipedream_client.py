"""Pipedream Connect client — managed-auth account connections + pre-built
actions, behind one flag.

This is the server side of an ALTERNATIVE connections surface (a test tab that
mirrors the native Google/Slack/Asana/Jira cards, but brokered through
Pipedream Connect instead of each vendor's own OAuth). It is INERT unless
PIPEDREAM_PROJECT_ID + PIPEDREAM_CLIENT_ID + PIPEDREAM_CLIENT_SECRET are all
set, so the key-free demo and existing deployments never touch it.

Auth is the documented Connect model: exchange the OAuth client_id/secret for a
1-hour access token (cached here), then call the project-scoped Connect API
with `Authorization: Bearer <token>` + `x-pd-environment`. The end user is the
Laura **org_id** (Pipedream's external_user_id), so connections are per-org,
matching the native model. Secrets and tokens are never logged.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

from .config import settings


class PipedreamError(RuntimeError):
    """A Connect API call failed — the caller degrades to 'not connected'."""


class PipedreamUnconfigured(PipedreamError):
    """The feature flag is off (missing project/client credentials)."""


def enabled() -> bool:
    """The one switch every entry point checks."""
    return bool(
        settings.pipedream_project_id
        and settings.pipedream_client_id
        and settings.pipedream_client_secret
    )


def environment() -> str:
    env = (settings.pipedream_environment or "development").strip().lower()
    return env if env in ("development", "production") else "development"


def _base() -> str:
    return (settings.pipedream_api_base or "https://api.pipedream.com/v1").rstrip("/")


# ── access token cache (client-credentials, 1h TTL) ─────────────────────────
_token_lock = threading.Lock()
_token_value = ""
_token_expiry = 0.0  # monotonic seconds


def _now() -> float:
    return time.monotonic()


def _fetch_access_token() -> str:
    """Exchange client_id/secret for an access token (grant_type=client_credentials)."""
    import httpx

    try:
        resp = httpx.post(
            f"{_base()}/oauth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": settings.pipedream_client_id,
                "client_secret": settings.pipedream_client_secret,
            },
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 — network/DNS/timeout
        raise PipedreamError("token exchange failed") from exc
    if resp.status_code >= 400:
        # Never echo the body verbatim (may reflect the secret); just the code.
        raise PipedreamError(f"token exchange http {resp.status_code}")
    data = resp.json() or {}
    token = str(data.get("access_token") or "")
    if not token:
        raise PipedreamError("token exchange returned no access_token")
    ttl = float(data.get("expires_in") or 3600)
    with _token_lock:
        global _token_value, _token_expiry
        _token_value = token
        # Refresh 60s early so an in-flight request never rides an expired token.
        _token_expiry = _now() + max(60.0, ttl - 60.0)
    return token


def _access_token() -> str:
    with _token_lock:
        if _token_value and _now() < _token_expiry:
            return _token_value
    return _fetch_access_token()


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_access_token()}",
        "x-pd-environment": environment(),
        "Content-Type": "application/json",
    }


def _project_url(path: str) -> str:
    pid = settings.pipedream_project_id
    return f"{_base()}/connect/{pid}/{path.lstrip('/')}"


def _request(method: str, path: str, *, json_body: Optional[dict] = None,
             params: Optional[dict] = None) -> dict:
    if not enabled():
        raise PipedreamUnconfigured("pipedream not configured")
    import httpx

    url = _project_url(path)
    try:
        resp = httpx.request(
            method, url, headers=_headers(), json=json_body, params=params,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        raise PipedreamError(f"{method} {path} failed") from exc
    if resp.status_code == 401:
        # Token may have been revoked/expired mid-cache; refresh once and retry.
        _fetch_access_token()
        try:
            resp = httpx.request(
                method, url, headers=_headers(), json=json_body, params=params,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            raise PipedreamError(f"{method} {path} retry failed") from exc
    if resp.status_code >= 400:
        raise PipedreamError(f"{method} {path} http {resp.status_code}")
    try:
        return resp.json() or {}
    except Exception:  # noqa: BLE001
        return {}


# ── managed auth: create a connect token + hosted Connect Link ───────────────

def create_connect_token(
    external_user_id: str, *, app: str = "",
    success_redirect_uri: str = "", error_redirect_uri: str = "",
    allowed_origins: Optional[list[str]] = None,
) -> dict:
    """Mint a short-lived connect token for one org (external_user_id).

    Returns {token, expires_at, connect_url}. ``connect_url`` is the hosted
    Connect Link the browser is sent to (redirect flow — no SDK). When ``app``
    is given the link is pre-scoped to that app slug so the user lands straight
    on its consent screen.
    """
    body: dict[str, Any] = {"external_user_id": str(external_user_id)}
    if success_redirect_uri:
        body["success_redirect_uri"] = success_redirect_uri
    if error_redirect_uri:
        body["error_redirect_uri"] = error_redirect_uri
    if allowed_origins:
        body["allowed_origins"] = list(allowed_origins)
    data = _request("POST", "tokens", json_body=body)
    token = str(data.get("token") or "")
    connect_url = str(data.get("connect_link_url") or "")
    # Pre-scope to the app slug when we have one (documented Connect Link param).
    if app and connect_url:
        sep = "&" if "?" in connect_url else "?"
        connect_url = f"{connect_url}{sep}app={app}"
    elif app and token:
        connect_url = (
            "https://pipedream.com/_static/connect.html"
            f"?token={token}&connectLink=true&app={app}"
        )
    return {
        "token": token,
        "expires_at": data.get("expires_at"),
        "connect_url": connect_url,
    }


# ── connected accounts (per org) ────────────────────────────────────────────

def list_accounts(external_user_id: str, *, app: str = "") -> list[dict]:
    """Connected accounts for one org. Credentials are never requested
    (include_credentials=false) — this is a status read only."""
    params: dict[str, Any] = {
        "external_user_id": str(external_user_id),
        "include_credentials": "false",
    }
    if app:
        params["app"] = app
    data = _request("GET", "accounts", params=params)
    accounts = data.get("data") if isinstance(data.get("data"), list) else data.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    # Distil to safe fields only (no tokens): the app slug, a display name,
    # the authProvisionId (needed to run actions), and health.
    out = []
    for a in accounts:
        if not isinstance(a, dict):
            continue
        app_field = a.get("app")
        slug = app_field.get("name_slug") if isinstance(app_field, dict) else app_field
        out.append({
            "id": a.get("id"),
            "app": slug or a.get("name_slug") or "",
            "name": a.get("name") or a.get("external_id") or "",
            "healthy": a.get("healthy", True),
        })
    return out


# ── run a pre-built action (the "pre-configured actions" surface) ────────────

def run_action(
    external_user_id: str, action_id: str, configured_props: dict,
) -> dict:
    """Execute one Connect action on the org's connected account. Returns the
    action's {exports, os, ret}. Distilled — never raw credentials."""
    body = {
        "external_user_id": str(external_user_id),
        "id": str(action_id),
        "configured_props": configured_props or {},
    }
    return _request("POST", "actions/run", json_body=body)


# ── low-level authed request (auto-retries once on a 401 token expiry) ───────
# Safe ONLY for idempotent calls (GET/DELETE): a blind retry must never be used
# on the proxy write path, where it could double-send a POST.
def _authed_request(method: str, url: str, *, params: Optional[dict] = None,
                    json_body: Optional[dict] = None, timeout: float = 30):
    if not enabled():
        raise PipedreamUnconfigured("pipedream not configured")
    import httpx

    try:
        resp = httpx.request(method, url, headers=_headers(), params=params,
                             json=json_body, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise PipedreamError(f"{method} failed") from exc
    if resp.status_code == 401:
        _fetch_access_token()
        try:
            resp = httpx.request(method, url, headers=_headers(), params=params,
                                 json=json_body, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise PipedreamError(f"{method} retry failed") from exc
    return resp


def _b64url_nopad(value: str) -> str:
    """base64url without padding — the Connect Proxy target-URL encoding."""
    import base64
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


# ── app logos (resolved by slug, cached per process) ────────────────────────
_logo_lock = threading.Lock()
_logo_cache: dict[str, str] = {}  # slug -> img url ("" = looked up, none found)


def logos_for(slugs: list[str]) -> dict[str, str]:
    """Resolve real catalog logo URLs for a set of app slugs (slug -> img).
    Cached per process; a miss is cached as "" so it isn't re-fetched. Best
    effort — a lookup failure just yields no logo for that slug."""
    out: dict[str, str] = {}
    missing: list[str] = []
    with _logo_lock:
        for s in slugs:
            s = str(s or "").strip().lower()
            if not s:
                continue
            if s in _logo_cache:
                if _logo_cache[s]:
                    out[s] = _logo_cache[s]
            elif s not in missing:
                missing.append(s)
    for s in missing:
        img = ""
        try:
            for app in search_apps(s, limit=8).get("apps", []):
                if app.get("slug") == s:
                    img = app.get("img") or ""
                    break
        except PipedreamError:
            img = ""
        with _logo_lock:
            _logo_cache[s] = img
        if img:
            out[s] = img
    return out


# ── app catalog search (the generic 3,000+ app grid) ────────────────────────

def search_apps(query: str = "", *, limit: int = 30, after: str = "") -> dict:
    """One page of Pipedream's app catalog (optionally free-text filtered).
    Returns {apps:[{slug,name,description,categories,img}], next_cursor, total}.
    NOTE: /connect/apps is NOT project-scoped."""
    limit = max(1, min(int(limit or 30), 100))
    params: dict[str, Any] = {"limit": str(limit)}
    if query:
        params["q"] = query
    if after:
        params["after"] = after
    resp = _authed_request("GET", f"{_base()}/connect/apps", params=params)
    if resp.status_code >= 400:
        raise PipedreamError(f"list apps http {resp.status_code}")
    data = resp.json() or {}
    raw = data.get("data")
    apps = []
    if isinstance(raw, list):
        for a in raw:
            if not isinstance(a, dict):
                continue
            slug = a.get("name_slug")
            if not slug:
                continue
            apps.append({
                "slug": slug,
                "name": a.get("name") or slug,
                "description": a.get("description") or "",
                "categories": a.get("categories") or [],
                "img": a.get("img_src") or "",
            })
    page = data.get("page_info") or {}
    # A short page is the last page; a full one may have more.
    next_cursor = str(page.get("end_cursor") or "") if len(apps) >= limit else ""
    return {"apps": apps, "next_cursor": next_cursor, "total": page.get("total_count")}


# ── Connect Proxy — run any authenticated REST call against a connected app ──
# Pipedream injects the account's credentials server-side; we send the target
# app's own API request. This is the generic execution path (mirrors Cedric's
# proxyGoogleCaller). NO auto-retry: a 401 is surfaced to the caller so a write
# is never blindly re-sent.

def proxy_request(external_user_id: str, account_id: str, method: str, url: str,
                  *, json_body: Optional[Any] = None,
                  headers: Optional[dict] = None, timeout: float = 30) -> dict:
    """Call ``url`` (the app's own API) through the Connect Proxy on the org's
    connected account. Returns {status, ok, json, text?}. Never raises on a
    downstream 4xx/5xx — the caller decides. Raises PipedreamError only on a
    transport failure or when the feature is unconfigured."""
    if not enabled():
        raise PipedreamUnconfigured("pipedream not configured")
    import json as _json

    import httpx

    proxy_url = _project_url(f"proxy/{_b64url_nopad(url)}")
    params = {"external_user_id": str(external_user_id), "account_id": str(account_id)}
    hdrs = {
        "Authorization": f"Bearer {_access_token()}",
        "x-pd-environment": environment(),
    }
    content = None
    if json_body is not None:
        hdrs["x-pd-proxy-Content-Type"] = "application/json"
        content = _json.dumps(json_body)
    # App-required headers the proxy forwards downstream (prefix stripped),
    # e.g. Notion-Version. Carried per-app by the mapper/playbooks.
    for k, v in (headers or {}).items():
        hdrs[f"x-pd-proxy-{k}"] = str(v)
    try:
        resp = httpx.request(method.upper(), proxy_url, headers=hdrs,
                             params=params, content=content, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise PipedreamError(f"proxy {method} failed") from exc
    out: dict[str, Any] = {"status": resp.status_code, "ok": resp.status_code < 400}
    if "application/json" in resp.headers.get("content-type", "").lower():
        try:
            out["json"] = resp.json()
        except Exception:  # noqa: BLE001
            out["json"] = None
            out["text"] = resp.text
    else:
        out["json"] = None
        out["text"] = resp.text
    return out


# ── revoke a connected account (idempotent) ─────────────────────────────────

def delete_account(account_id: str) -> bool:
    """Delete a connected account on Pipedream (revokes the stored grant).
    Idempotent: an already-gone account (404) counts as success. Best effort."""
    if not (enabled() and account_id):
        return False
    from urllib.parse import quote
    try:
        resp = _authed_request(
            "DELETE", _project_url(f"accounts/{quote(str(account_id), safe='')}"),
        )
    except PipedreamError:
        return False
    return resp.status_code < 400 or resp.status_code == 404
