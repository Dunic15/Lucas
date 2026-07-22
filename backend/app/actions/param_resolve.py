"""Semantic parameter validation against the org's REAL tool data.

Shape validation (action_plane.validate_param_edits) accepts any string, so a
card could reach Approve with project="jj" / assignee="hh" and die at the
vendor with an opaque 400 (live card e44f90f7ee62497d, 2026-07-22). This
module is the missing gate: before an action becomes approvable, entity-like
fields must RESOLVE against the org's actual workspace — and once resolved,
the args are rewritten to canonical ids so the executor never re-guesses.

Contract:
- ``validate_typed_params(org, typed)`` -> (rewrites, errors)
  * rewrites: {field: canonical_value} — resolved ids to persist (may be {})
  * errors:   [{"field", "message", "options": [display names]}] — non-empty
              means the action must NOT be approvable yet.
- FAIL-OPEN by design: if the org's data cannot be listed at all (no token,
  proxy down), we return no errors — we cannot validate what we cannot see,
  and blocking every approve on a listing hiccup would be worse. The honest
  4xx receipt remains the backstop.
- Read-only, cached briefly, never raises, never logs transcript content.
"""
from __future__ import annotations

import time
from typing import Any

_CACHE_TTL_S = 120.0
_cache: dict[str, tuple[float, dict]] = {}

_MAX_OPTIONS = 20


def _cached(key: str) -> dict | None:
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL_S:
        return hit[1]
    return None


def _store(key: str, value: dict) -> dict:
    _cache[key] = (time.time(), value)
    if len(_cache) > 64:
        for stale in sorted(_cache, key=lambda k: _cache[k][0])[: len(_cache) - 64]:
            _cache.pop(stale, None)
    return value


def _pd_asana_account(org_id: str) -> str:
    try:
        from .. import pipedream_client

        for account in pipedream_client.list_accounts(org_id, app="asana"):
            if account.get("id"):
                return str(account["id"])
    except Exception:  # noqa: BLE001
        pass
    return ""


def _pd_get(org_id: str, account_id: str, url: str) -> list[dict]:
    """Proxy GET returning Asana's `data` list; [] on any failure."""
    try:
        from .. import pipedream_client

        resp = pipedream_client.proxy_request(org_id, account_id, "GET", url)
        if not resp.get("ok"):
            return []
        data = (resp.get("json") or {}).get("data") or []
        return [d for d in data if isinstance(d, dict)]
    except Exception:  # noqa: BLE001
        return []


def asana_directory(org_id: str) -> dict:
    """{"projects": [{gid,name}], "users": [{gid,name,email}], "ok": bool}.

    ok=False means NO plane could list anything (unknown workspace) — callers
    fail open. Native token first, Pipedream Connect proxy second: the same
    cutover rule as execution, so validation sees the same world the executor
    will act on.
    """
    key = f"asana:{org_id}"
    hit = _cached(key)
    if hit is not None:
        return hit

    from ..integrations import asana_client

    projects: list[dict] = []
    users: list[dict] = []
    native_p = asana_client.list_projects(org_id, max_results=100)
    if native_p.get("ok"):
        projects = list(native_p.get("projects") or [])
    native_u = asana_client.list_users(org_id, max_results=100)
    if native_u.get("ok"):
        users = list(native_u.get("users") or [])

    if not projects or not users:
        account = _pd_asana_account(org_id)
        if account:
            try:
                from .. import pipedream_executor as _pe

                api = _pe._ASANA_API
                ws = _pe._asana_workspace(org_id, account)
            except Exception:  # noqa: BLE001
                api, ws = "", ""
            if ws:
                if not projects:
                    projects = [
                        {"gid": str(p.get("gid") or ""),
                         "name": str(p.get("name") or "")[:120]}
                        for p in _pd_get(
                            org_id, account,
                            f"{api}/projects?workspace={ws}&archived=false&limit=100",
                        )
                    ]
                if not users:
                    users = [
                        {"gid": str(u.get("gid") or ""),
                         "name": str(u.get("name") or "")[:120],
                         "email": str(u.get("email") or "")[:200]}
                        for u in _pd_get(
                            org_id, account,
                            f"{api}/workspaces/{ws}/users?opt_fields=name,email&limit=100",
                        )
                    ]

    result = {
        "ok": bool(projects or users),
        "projects": [p for p in projects if p.get("gid")],
        "users": [u for u in users if u.get("gid")],
    }
    return _store(key, result)


def _match(needle: str, rows: list[dict], *fields: str) -> str:
    """Exact (case-insensitive) match first, then unambiguous substring."""
    want = needle.strip().lower()
    for row in rows:
        for f in fields:
            if str(row.get(f) or "").strip().lower() == want:
                return str(row.get("gid") or "")
    partial = [
        str(row.get("gid") or "")
        for row in rows
        if any(want in str(row.get(f) or "").strip().lower() for f in fields)
    ]
    return partial[0] if len(partial) == 1 else ""


def validate_typed_params(org_id: str, typed: dict | None) -> tuple[dict, list[dict]]:
    """Resolve entity fields for the families we know; see module docstring."""
    if not isinstance(typed, dict):
        return {}, []
    action_type = str(typed.get("type") or "")
    if not action_type.startswith("asana."):
        return {}, []
    args = typed.get("args") if isinstance(typed.get("args"), dict) else {}

    checks: list[tuple[str, str]] = []  # (field, kind)
    project = str(args.get("project") or "").strip()
    if project and not project.isdigit():
        checks.append(("project", "project"))
    assignee = str(args.get("assignee") or "").strip()
    if assignee and assignee.lower() != "me" and not assignee.isdigit():
        checks.append(("assignee", "user"))
    if not checks:
        return {}, []

    directory = asana_directory(org_id)
    if not directory.get("ok"):
        return {}, []  # cannot see the workspace → fail open

    rewrites: dict[str, Any] = {}
    errors: list[dict] = []
    for field, kind in checks:
        value = str(args.get(field) or "").strip()
        if kind == "project":
            rows, fields_, label = directory["projects"], ("name",), "project"
            options = [p["name"] for p in directory["projects"]][:_MAX_OPTIONS]
        else:
            rows, fields_, label = directory["users"], ("name", "email"), "person"
            options = [u["name"] or u["email"] for u in directory["users"]][:_MAX_OPTIONS]
        gid = _match(value, rows, *fields_)
        if gid:
            rewrites[field] = gid
        elif rows:
            errors.append({
                "field": field,
                "message": f"no Asana {label} matches {value!r}",
                "options": options,
            })
        # rows empty for this kind → that listing failed; fail open on it
    return rewrites, errors
