"""Asana — the org's project system of record (read + write).

The first task-tool connector from the commercial roadmap ("Task-tool push —
actions become tickets"): given an org that connected Asana, read the
workspace (projects, open/overdue tasks) into a compact meeting brief, and
execute approved task actions (create / update / comment) as the org.

Auth (simplest thing that works, upgradeable to OAuth without rework):
a Personal Access Token, resolved per org — the encrypted per-org row
(``store.set_org_oauth(org, pat, provider="asana")``) wins, with the
``ASANA_TOKEN`` env var as the single-tenant fallback. The default workspace
is ``ASANA_WORKSPACE_GID``, auto-discovered from the token when unset.

Contract — identical to ``google_client``:
- Takes ``org_id`` + a plain dict of already-distilled fields (never
  transcript content). A wrong/absent org yields "not connected", never a
  call from the wrong account.
- Returns ``{"ok": True, ...provenance...}`` or ``{"ok": False, "error"}``.
  NEVER raises: this runs at session start / finalize / approval, off the
  live-meeting path — an Asana hiccup must degrade softly.
- Logs no token and no task content.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from .. import store
from ..config import settings

_API = "https://app.asana.com/api/1.0"
_OAUTH_TOKEN_URL = "https://app.asana.com/-/oauth_token"
_TIMEOUT = 20.0

# OAuth access-token cache (the "Connect Asana" button path): Asana OAuth
# access tokens live ~1h; mint once per org per hour instead of per call.
# Same contract as google_client's cache — in-process only, never logged.
_oauth_lock = threading.Lock()
_OAUTH_CACHE: dict[str, tuple[str, float]] = {}  # org_id -> (token, expires_at)
_OAUTH_EXPIRY_MARGIN_S = 120.0
_OAUTH_DEFAULT_TTL_S = 3300.0

# Workspace-brief cache: the snapshot is fetched at session start (join) and
# would otherwise refetch on every join of a busy org. Same shape as
# drive_client's folder cache.
_BRIEF_TTL_SECONDS = 600.0
_brief_cache: dict[str, tuple[float, str]] = {}
_brief_lock = threading.Lock()

# Caps that keep the join-time brief cheap: it rides the live prompt.
_BRIEF_MAX_PROJECTS = 8
_BRIEF_MAX_TASKS_PER_PROJECT = 6
_BRIEF_MAX_CHARS = 2400


def oauth_available() -> bool:
    """True when the Asana OAuth app is configured (the one-click connect)."""
    return bool(settings.asana_client_id and settings.asana_client_secret)


def _token(org_id: str) -> tuple[str, str]:
    """(bearer_token, "") for the org, or ("", error).

    Precedence: the OAuth grant from the dashboard's "Connect Asana" button
    (provider="asana-oauth" — a refresh token minted into short-lived access
    tokens, cached) → the pasted PAT (provider="asana") → the ASANA_TOKEN env
    fallback. An expired/unmintable OAuth grant falls through rather than
    masking a working PAT."""
    org = (org_id or "").strip()
    try:
        oauth_row = store.get_org_oauth(org, provider="asana-oauth")
    except Exception:  # noqa: BLE001 — a store hiccup reads as not connected
        oauth_row = None
    if oauth_row and oauth_row.get("refresh_token"):
        tok, _err = _oauth_access_token(org, oauth_row)
        if tok:
            return tok, ""
    try:
        row = store.get_org_oauth(org, provider="asana")
    except Exception:  # noqa: BLE001
        row = None
    pat = (row or {}).get("refresh_token", "")
    if not pat and _env_pat_allowed(org):
        # The ASANA_TOKEN env PAT is the DEPLOYMENT owner's own workspace. It
        # may serve only the deployment's own (demo/key-free) org — never an
        # arbitrary tenant, which would execute that tenant's approved actions
        # against someone else's Asana (security audit 2026-07-23, gap #2).
        pat = settings.asana_token.strip()
    if not pat:
        return "", "Asana is not connected for this org"
    return pat, ""


def _env_pat_allowed(org_id: str) -> bool:
    """May this org fall back to the deployment-wide ASANA_TOKEN?

    Only the deployment's own org (the key-free demo tenant) may: the env PAT
    authenticates the DEPLOYMENT OWNER's Asana workspace, so handing it to a
    real tenant would run that tenant's approved writes inside the owner's
    workspace — cross-tenant execution with a truthful-looking receipt.
    """
    org = (org_id or "").strip()
    return not org or org == str(settings.demo_org_id or "").strip()


def _oauth_access_token(org_id: str, row: dict) -> tuple[str, str]:
    """Mint (or serve cached) a short-lived access token from the org's OAuth
    refresh token. ("", error) on any failure — the caller falls through."""
    now = time.time()
    with _oauth_lock:
        cached = _OAUTH_CACHE.get(org_id)
        if cached and cached[1] > now:
            return cached[0], ""
    if not oauth_available():
        return "", "Asana OAuth app is not configured"
    try:
        resp = httpx.post(
            _OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": settings.asana_client_id,
                "client_secret": settings.asana_client_secret,
                "refresh_token": row["refresh_token"],
            },
            timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return "", f"asana token request failed ({type(e).__name__})"
    if resp.status_code >= 300:
        return "", f"asana token refresh rejected (HTTP {resp.status_code})"
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return "", "asana token endpoint returned no JSON"
    tok = str(data.get("access_token") or "")
    if not tok:
        return "", "no access token returned"
    try:
        ttl = float(data.get("expires_in") or 0)
    except (TypeError, ValueError):
        ttl = 0.0
    if ttl <= 0:
        ttl = _OAUTH_DEFAULT_TTL_S
    with _oauth_lock:
        _OAUTH_CACHE[org_id] = (tok, now + max(60.0, ttl - _OAUTH_EXPIRY_MARGIN_S))
    # Persist a rotated refresh token (never discard — that strands the grant).
    new_rt = str(data.get("refresh_token") or "").strip()
    if new_rt and new_rt != row["refresh_token"]:
        try:
            store.set_org_oauth(
                org_id, new_rt, provider="asana-oauth",
                email=str(row.get("email") or ""),
                scopes=str(row.get("scopes") or ""),
            )
        except Exception:  # noqa: BLE001 — best-effort; the old rt may still work
            pass
    return tok, ""


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Authorization-code exchange for the OAuth callback: {"ok",
    "refresh_token", "access_token", "email", "name"} or {"ok": False,
    "error"}. Never raises, never logs tokens."""
    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "code is required"}
    if not oauth_available():
        return {"ok": False, "error": "Asana OAuth app is not configured"}
    try:
        resp = httpx.post(
            _OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": settings.asana_client_id,
                "client_secret": settings.asana_client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"asana token request failed ({type(e).__name__})"}
    if resp.status_code >= 300:
        return {"ok": False, "error": f"asana code exchange failed (HTTP {resp.status_code})"}
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "asana token endpoint returned no JSON"}
    user = data.get("data") or {}
    return {
        "ok": True,
        "access_token": str(data.get("access_token") or ""),
        "refresh_token": str(data.get("refresh_token") or ""),
        "email": str(user.get("email") or "")[:120],
        "name": str(user.get("name") or "")[:120],
    }


def connected(org_id: str) -> bool:
    """True when this org can reach Asana (per-org token or env fallback)."""
    return bool(_token(org_id)[0])


def verify_token(pat: str) -> dict:
    """Live check of a PAT for the dashboard connect flow: who it
    authenticates as and which workspace it sees. {"ok", "email",
    "workspace", "workspace_gid"} or {"ok": False, "error"}. Never raises,
    never logs or returns the token."""
    pat = (pat or "").strip()
    if not pat:
        return {"ok": False, "error": "token is required"}
    me, err = _get(pat, "/users/me", {"opt_fields": "email,name,workspaces.name"})
    if err:
        return {"ok": False, "error": err}
    workspaces = (me or {}).get("workspaces") or []
    first = workspaces[0] if workspaces else {}
    if not first:
        return {"ok": False, "error": "the token sees no workspaces"}
    return {
        "ok": True,
        "email": str((me or {}).get("email") or "")[:120],
        "workspace": str(first.get("name") or "")[:120],
        "workspace_gid": str(first.get("gid") or ""),
    }


def _headers(pat: str) -> dict:
    return {"Authorization": f"Bearer {pat}", "Accept": "application/json"}


def _get(pat: str, path: str, params: dict | None = None) -> tuple[Any, str]:
    """(data, "") or (None, error) for one Asana GET. Soft errors only."""
    try:
        resp = httpx.get(
            f"{_API}{path}", params=params or {}, headers=_headers(pat),
            timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"asana request failed ({type(e).__name__})"
    if resp.status_code == 401:
        return None, "asana rejected the token (HTTP 401) — reconnect"
    if resp.status_code >= 300:
        return None, f"asana GET {path.split('?')[0]} failed (HTTP {resp.status_code})"
    try:
        return resp.json().get("data"), ""
    except Exception:  # noqa: BLE001
        return None, "asana returned no JSON"


def _workspace_gid(pat: str) -> tuple[str, str]:
    """The workspace to operate in: the configured gid, else the token's first
    workspace (most PATs see exactly one)."""
    gid = settings.asana_workspace_gid.strip()
    if gid:
        return gid, ""
    data, err = _get(pat, "/workspaces", {"limit": 1})
    if err:
        return "", err
    if not data:
        return "", "the Asana token sees no workspaces"
    return str(data[0].get("gid") or ""), ""


# ─────────────────────────── reads ───────────────────────────

def list_projects(org_id: str, *, max_results: int = 30) -> dict:
    """Non-archived projects in the workspace: {"ok", "projects": [{gid, name}]}."""
    pat, err = _token(org_id)
    if err:
        return {"ok": False, "error": err}
    ws, err = _workspace_gid(pat)
    if err:
        return {"ok": False, "error": err}
    data, err = _get(
        pat, "/projects",
        {"workspace": ws, "archived": "false",
         "limit": max(1, min(int(max_results or 30), 100))},
    )
    if err:
        return {"ok": False, "error": err}
    return {
        "ok": True,
        "workspace_gid": ws,
        "projects": [
            {"gid": str(p.get("gid") or ""), "name": str(p.get("name") or "")}
            for p in (data or [])
            if isinstance(p, dict)
        ],
    }


def list_users(org_id: str, *, max_results: int = 50) -> dict:
    """Workspace members: {"ok", "users": [{gid, name, email}]}. Same contract
    as list_projects — never raises, {"ok": False, "error"} on any failure."""
    pat, err = _token(org_id)
    if err:
        return {"ok": False, "error": err}
    ws, err = _workspace_gid(pat)
    if err:
        return {"ok": False, "error": err}
    data, err = _get(
        pat, f"/workspaces/{ws}/users",
        {"opt_fields": "name,email",
         "limit": max(1, min(int(max_results or 50), 100))},
    )
    if err:
        return {"ok": False, "error": err}
    return {
        "ok": True,
        "users": [
            {"gid": str(u.get("gid") or ""),
             "name": str(u.get("name") or "")[:120],
             "email": str(u.get("email") or "")[:200]}
            for u in (data or [])
            if isinstance(u, dict)
        ],
    }


def _resolve_project(org_id: str, project: str) -> tuple[str, str]:
    """A project gid from a gid-or-name string; ("", err) when unresolvable."""
    project = (project or "").strip()
    if not project:
        return "", ""  # optional everywhere it's used
    if project.isdigit():
        return project, ""
    listing = list_projects(org_id, max_results=100)
    if not listing.get("ok"):
        return "", str(listing.get("error") or "project lookup failed")
    want = project.lower()
    for p in listing["projects"]:
        if p["name"].strip().lower() == want:
            return p["gid"], ""
    for p in listing["projects"]:
        if want in p["name"].strip().lower():
            return p["gid"], ""
    return "", f"no Asana project matches {project!r}"


def project_tasks(org_id: str, project: str, *, max_results: int = 20) -> dict:
    """Open tasks in one project (gid or name): {"ok", "tasks": [...]}. Each
    task: {gid, name, assignee, due_on, completed}."""
    pat, err = _token(org_id)
    if err:
        return {"ok": False, "error": err}
    gid, err = _resolve_project(org_id, project)
    if err or not gid:
        return {"ok": False, "error": err or "project is required"}
    data, err = _get(
        pat, "/tasks",
        {"project": gid, "completed_since": "now",
         "opt_fields": "name,due_on,completed,assignee.name",
         "limit": max(1, min(int(max_results or 20), 100))},
    )
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "project_gid": gid, "tasks": [_task_row(t) for t in data or []]}


def find_tasks(org_id: str, query: str, *, max_results: int = 10) -> dict:
    """Task typeahead search across the workspace: {"ok", "tasks": [...]}."""
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    pat, err = _token(org_id)
    if err:
        return {"ok": False, "error": err}
    ws, err = _workspace_gid(pat)
    if err:
        return {"ok": False, "error": err}
    data, err = _get(
        pat, f"/workspaces/{ws}/typeahead",
        {"resource_type": "task", "query": query[:100],
         "opt_fields": "name,due_on,completed,assignee.name",
         "count": max(1, min(int(max_results or 10), 25))},
    )
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "tasks": [_task_row(t) for t in data or []]}


def _task_row(t: Any) -> dict:
    t = t if isinstance(t, dict) else {}
    assignee = t.get("assignee") or {}
    return {
        "gid": str(t.get("gid") or ""),
        "name": str(t.get("name") or "")[:200],
        "assignee": str((assignee or {}).get("name") or "")[:80],
        "due_on": str(t.get("due_on") or ""),
        "completed": bool(t.get("completed")),
    }


def _pd_asana_account(org_id: str):
    """(account_id, pipedream_client_module) for the org's Pipedream-connected
    Asana, or ("", None). Post-cutover Asana runs through Pipedream, so the
    board snapshot must read there too (managed OAuth — no raw PAT here)."""
    try:
        from .. import pipedream_client, pipedream_executor
    except Exception:  # noqa: BLE001
        return "", None
    if not (pipedream_executor.enabled()
            and pipedream_executor.app_connected(org_id, "asana")):
        return "", None
    try:
        accts = pipedream_client.list_accounts(org_id, app="asana")
    except pipedream_client.PipedreamError:
        return "", None
    acct = next((a for a in accts if a.get("id")), None)
    return (str(acct["id"]) if acct else ""), pipedream_client


def _pd_get(pc, org_id: str, acct: str, path: str, params: dict | None):
    from urllib.parse import urlencode
    url = f"{_API}{path}" + (("?" + urlencode(params)) if params else "")
    try:
        r = pc.proxy_request(org_id, acct, "GET", url)
    except Exception:  # noqa: BLE001
        return None
    if not r.get("ok"):
        return None
    return (r.get("json") or {}).get("data")


def _build_brief_pd(org_id: str, acct: str, pc) -> str:
    """Same distilled board snapshot as _build_brief, read via the Connect Proxy."""
    ws_data = _pd_get(pc, org_id, acct, "/workspaces", None) or []
    ws = str(ws_data[0].get("gid") or "") if ws_data else ""
    if not ws:
        return ""
    projects = _pd_get(pc, org_id, acct, "/projects",
                       {"workspace": ws, "archived": "false",
                        "limit": _BRIEF_MAX_PROJECTS}) or []
    today = time.strftime("%Y-%m-%d")
    lines: list[str] = [f"(as of {today})"]
    for p in projects[:_BRIEF_MAX_PROJECTS]:
        gid = str(p.get("gid") or "")
        tasks = _pd_get(pc, org_id, acct, "/tasks",
                        {"project": gid,
                         "opt_fields": "name,completed,assignee.name,due_on",
                         "limit": _BRIEF_MAX_TASKS_PER_PROJECT}) or []
        open_tasks = [t for t in tasks if isinstance(t, dict) and not t.get("completed")]
        lines.append(f"• {str(p.get('name') or '')} — {len(open_tasks)} open task(s)")
        for t in open_tasks[:_BRIEF_MAX_TASKS_PER_PROJECT]:
            bits = [str(t.get("name") or "")]
            asg = t.get("assignee") or {}
            if isinstance(asg, dict) and asg.get("name"):
                bits.append(f"owner: {asg['name']}")
            if t.get("due_on"):
                overdue = str(t["due_on"]) < today
                bits.append(f"due {t['due_on']}" + (" (OVERDUE)" if overdue else ""))
            lines.append("   - " + " · ".join(bits))
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)[:_BRIEF_MAX_CHARS]


def workspace_brief(org_id: str) -> str:
    """Markdown snapshot of the workspace for the meeting brief; "" when Asana
    is unavailable. Reads through the org's Pipedream-connected Asana when it has
    one (the cutover path), else the native PAT/OAuth. Sync (network) — call via
    run_in_threadpool at session start only. TTL-cached per org.

    Content is distilled and capped (project names, open task names, owners,
    due dates, an OVERDUE flag) — it rides the live prompt, so it must stay
    cheap, and it must never carry anything but workspace facts."""
    key = (org_id or "").strip() or "-"
    now = time.time()
    with _brief_lock:
        cached = _brief_cache.get(key)
        if cached is not None and now - cached[0] < _BRIEF_TTL_SECONDS:
            return cached[1]

    acct, pc = _pd_asana_account(org_id)
    if acct:
        text = _build_brief_pd(org_id, acct, pc)
    else:
        # Pipedream-connected but the account didn't resolve THIS instant is a
        # transient proxy hiccup, not "no Asana" — return "" UNCACHED so the next
        # join retries, instead of masking it with the guaranteed-empty native
        # path (which would then get negative-cached below).
        try:
            from .. import pipedream_executor

            if pipedream_executor.enabled() and pipedream_executor.app_connected(
                org_id, "asana"
            ):
                return ""
        except Exception:  # noqa: BLE001 — Pipedream absence is not an error
            pass
        text = _build_brief(org_id)
    with _brief_lock:
        # NEVER negative-cache. One empty/transient read must not poison the
        # per-org snapshot for the whole 10-min TTL (live 2026-07-23: Petra said
        # "no snapshot for this meeting" for minutes after a single empty read).
        # Only a real snapshot is cached; an empty result is retried next join.
        if text:
            _brief_cache[key] = (now, text)
        else:
            _brief_cache.pop(key, None)
    return text


def _build_brief(org_id: str) -> str:
    listing = list_projects(org_id, max_results=_BRIEF_MAX_PROJECTS)
    if not listing.get("ok"):
        return ""
    today = time.strftime("%Y-%m-%d")
    lines: list[str] = [f"(as of {today})"]
    for p in listing["projects"][:_BRIEF_MAX_PROJECTS]:
        tasks = project_tasks(
            org_id, p["gid"], max_results=_BRIEF_MAX_TASKS_PER_PROJECT
        )
        if not tasks.get("ok"):
            continue
        open_tasks = [t for t in tasks["tasks"] if not t["completed"]]
        lines.append(f"• {p['name']} — {len(open_tasks)} open task(s)")
        for t in open_tasks[:_BRIEF_MAX_TASKS_PER_PROJECT]:
            bits = [t["name"]]
            if t["assignee"]:
                bits.append(f"owner: {t['assignee']}")
            if t["due_on"]:
                overdue = t["due_on"] < today
                bits.append(f"due {t['due_on']}" + (" (OVERDUE)" if overdue else ""))
            lines.append("   - " + " · ".join(bits))
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)[:_BRIEF_MAX_CHARS]


def _reset_brief_cache() -> None:
    """Test seam — process-global state (the workspace-brief cache AND the
    OAuth access-token cache), cleared per test (see conftest) and on every
    connect/disconnect so a new grant never serves the old org's view."""
    with _brief_lock:
        _brief_cache.clear()
    with _oauth_lock:
        _OAUTH_CACHE.clear()


# ─────────────────────────── writes ───────────────────────────

def create_task(org_id: str, task: dict) -> dict:
    """Create a task: {name (required), notes?, project (gid|name)?,
    assignee (email|gid)?, due_on (YYYY-MM-DD)?}. Returns the permalink as
    the provenance receipt."""
    name = str(task.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "task needs a name"}
    pat, err = _token(org_id)
    if err:
        return {"ok": False, "error": err}
    ws, err = _workspace_gid(pat)
    if err:
        return {"ok": False, "error": err}

    body: dict[str, Any] = {"name": name[:300], "workspace": ws}
    if task.get("notes"):
        body["notes"] = str(task["notes"])[:4000]
    if task.get("assignee"):
        body["assignee"] = str(task["assignee"]).strip()
    else:
        # No assignee in the spec → default to the CONNECTED account ("me" =
        # the token's user). A task with no assignee and no project appears in
        # NO Asana view (not My Tasks, not any project) — a real but invisible
        # orphan (live finding 2026-07-17: voice-created task nobody could
        # find). Defaulting to the connection owner puts every created task in
        # someone's My Tasks; the meeting flow can still assign someone else.
        body["assignee"] = "me"
    if task.get("due_on"):
        body["due_on"] = str(task["due_on"]).strip()[:10]
    project_gid, perr = _resolve_project(org_id, str(task.get("project") or ""))
    if perr:
        return {"ok": False, "error": perr}
    if project_gid:
        body["projects"] = [project_gid]

    result = _post(
        pat, "/tasks", body,
        params={"opt_fields": "gid,name,permalink_url"},
        what="task",
    )
    gid = str(result.get("task_gid") or "")
    if result.get("ok") and gid:
        extras = _apply_task_extras(pat, ws, gid, task)
        if extras:
            result["extras"] = extras
    return result


def _resolve_task_ref(pat: str, ws: str, ref: str) -> str:
    """A dependency reference -> task gid: digits pass through; names go
    through workspace typeahead (first hit). "" when unresolvable."""
    ref = str(ref or "").strip()
    if not ref:
        return ""
    if ref.isdigit():
        return ref
    data, err = _get(pat, f"/workspaces/{ws}/typeahead",
                     {"resource_type": "task", "query": ref[:100], "count": 1})
    if err or not isinstance(data, list) or not data:
        return ""
    return str((data[0] or {}).get("gid") or "")


def _apply_task_extras(pat: str, ws: str, gid: str, task: dict) -> str:
    """Best-effort application of the fuller task spec after creation:
    subtasks (child tasks), dependencies (addDependencies), attachments
    (external URL attachments). The parent task already exists — an extras
    failure never fails the action; it is reported in the receipt detail."""
    applied: list[str] = []
    failed: list[str] = []
    subs = [str(x).strip() for x in (task.get("subtasks") or []) if str(x).strip()]
    for sub in subs[:10]:
        r = _post(pat, f"/tasks/{gid}/subtasks", {"name": sub[:300]}, what="subtask")
        (applied if r.get("ok") else failed).append(f"subtask:{sub[:40]}")
    dep_gids = []
    for dep in [str(x).strip() for x in (task.get("dependencies") or []) if str(x).strip()][:10]:
        dgid = _resolve_task_ref(pat, ws, dep)
        if dgid:
            dep_gids.append(dgid)
        else:
            failed.append(f"dependency:{dep[:40]}")
    if dep_gids:
        r = _post(pat, f"/tasks/{gid}/addDependencies", {"dependencies": dep_gids},
                  what="dependencies")
        (applied if r.get("ok") else failed).append(f"{len(dep_gids)} dependencies")
    for url in [str(x).strip() for x in (task.get("attachments") or []) if str(x).strip()][:10]:
        if not url.lower().startswith(("http://", "https://")):
            failed.append("attachment:non-url")
            continue
        r = _post(pat, "/attachments",
                  {"url": url, "parent": gid, "resource_subtype": "external",
                   "name": url.rsplit("/", 1)[-1][:100] or "attachment"},
                  what="attachment")
        (applied if r.get("ok") else failed).append("attachment")
    parts = []
    if applied:
        parts.append("applied " + ", ".join(applied[:6]))
    if failed:
        parts.append("FAILED " + ", ".join(failed[:6]))
    return "; ".join(parts)


def update_task(org_id: str, update: dict) -> dict:
    """Update a task by gid: {task (gid, required), completed?, due_on?,
    assignee?, name?}."""
    gid = str(update.get("task") or update.get("task_gid") or "").strip()
    if not gid.isdigit():
        return {"ok": False, "error": "update needs the task gid"}
    pat, err = _token(org_id)
    if err:
        return {"ok": False, "error": err}
    body: dict[str, Any] = {}
    if "completed" in update:
        body["completed"] = bool(update["completed"])
    if update.get("due_on"):
        body["due_on"] = str(update["due_on"]).strip()[:10]
    if update.get("assignee"):
        body["assignee"] = str(update["assignee"]).strip()
    if update.get("name"):
        body["name"] = str(update["name"]).strip()[:300]
    if not body:
        return {"ok": False, "error": "update carries no changes"}
    return _post(
        pat, f"/tasks/{gid}", body,
        params={"opt_fields": "gid,name,permalink_url"},
        what="task update", method="PUT",
    )


def add_comment(org_id: str, comment: dict) -> dict:
    """Comment on a task: {task (gid, required), text (required)}."""
    gid = str(comment.get("task") or comment.get("task_gid") or "").strip()
    text = str(comment.get("text") or comment.get("body") or "").strip()
    if not gid.isdigit():
        return {"ok": False, "error": "comment needs the task gid"}
    if not text:
        return {"ok": False, "error": "comment needs text"}
    pat, err = _token(org_id)
    if err:
        return {"ok": False, "error": err}
    return _post(pat, f"/tasks/{gid}/stories", {"text": text[:4000]}, what="comment")


def _post(
    pat: str, path: str, body: dict, *, params: dict | None = None,
    what: str = "request", method: str = "POST",
) -> dict:
    try:
        resp = httpx.request(
            method, f"{_API}{path}", params=params or {},
            headers=_headers(pat), json={"data": body}, timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"asana request failed ({type(e).__name__})"}
    if resp.status_code >= 300:
        return {"ok": False, "error": f"asana {what} failed (HTTP {resp.status_code})"}
    try:
        data = resp.json().get("data") or {}
    except Exception:  # noqa: BLE001
        data = {}
    gid = str(data.get("gid") or "")
    return {
        "ok": True,
        "task_gid": gid,
        "task_url": str(
            data.get("permalink_url")
            or (f"https://app.asana.com/0/0/{gid}/f" if gid else "")
        ),
        "name": str(data.get("name") or "")[:200],
    }
