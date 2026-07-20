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
