"""Pipedream-backed execution plane (Connect Proxy).

Parallel to native_runtime/executor.py, selected by ``execution_route ==
"pipedream"``. Runs an approved, TYPED action as a DETERMINISTIC REST call to the
app's own API through the Pipedream Connect Proxy (Pipedream injects the
account's credentials server-side). Core-app writes are NEVER model-constructed —
they use the fixed mapper below; model-constructed requests are for reads + the
low-stakes long tail only (a later phase).

Never raises: missing connections, bad arguments, and vendor failures all become
truthful ``failed`` ledger receipts, exactly like ``executor.execute_approved``.

Gated by ``settings.pipedream_executor`` AND the Pipedream feature flag
(``pipedream_client.enabled()``); default OFF, so prod behaviour is byte-identical
until it is flipped on.

Scope (refined 2026-07-20): Pipedream owns Asana + Jira + the long tail. The whole
Google block (calendar/gmail/drive) stays on Laura's native executor; Slack stays
on Cedric. So today this plane maps only the Asana action types — the ones being
moved off the native Asana adapter.
"""
from __future__ import annotations

from typing import Any, Callable

from . import ledger, pipedream_client
from .config import settings

# builder(org_id, account_id, args) -> (method, url, json_body|None, extra_headers|None)
# A builder may itself make read-only proxy calls (e.g. resolve the Asana
# workspace). It raises ValueError with a human-readable reason on bad arguments.
Builder = Callable[[str, str, dict], tuple]
ReceiptFn = Callable[[str, dict], tuple]

_ASANA_API = "https://app.asana.com/api/1.0"


# ── Asana request builders (faithful ports of asana_client) ─────────────────

def _asana_workspace(org_id: str, account_id: str) -> str:
    """Resolve the connected account's first workspace gid via the proxy."""
    resp = pipedream_client.proxy_request(
        org_id, account_id, "GET", f"{_ASANA_API}/workspaces",
    )
    if not resp.get("ok"):
        return ""
    data = (resp.get("json") or {}).get("data") or []
    if data and isinstance(data[0], dict):
        return str(data[0].get("gid") or "")
    return ""


def _build_asana_create(org_id: str, account_id: str, args: dict) -> tuple:
    name = str(args.get("name") or "").strip()
    if not name:
        raise ValueError("task needs a name")
    body: dict[str, Any] = {"name": name[:300]}
    if args.get("notes"):
        body["notes"] = str(args["notes"])[:4000]
    # Default the assignee to the connection owner ("me") so a task with no
    # project isn't an invisible orphan — same rule as the native adapter.
    body["assignee"] = str(args.get("assignee") or "me").strip()
    if args.get("due_on"):
        body["due_on"] = str(args["due_on"]).strip()[:10]
    project = str(args.get("project") or "").strip()
    if project.isdigit():
        body["projects"] = [project]
    else:
        # No project gid ⇒ Asana requires an explicit workspace.
        ws = _asana_workspace(org_id, account_id)
        if not ws:
            raise ValueError("couldn't resolve the Asana workspace")
        body["workspace"] = ws
    url = f"{_ASANA_API}/tasks?opt_fields=gid,name,permalink_url"
    return "POST", url, {"data": body}, None


def _build_asana_update(org_id: str, account_id: str, args: dict) -> tuple:
    gid = str(args.get("task") or args.get("task_gid") or "").strip()
    if not gid.isdigit():
        raise ValueError("update needs the task gid")
    body: dict[str, Any] = {}
    if "completed" in args:
        body["completed"] = bool(args["completed"])
    if args.get("due_on"):
        body["due_on"] = str(args["due_on"]).strip()[:10]
    if args.get("assignee"):
        body["assignee"] = str(args["assignee"]).strip()
    if args.get("name"):
        body["name"] = str(args["name"]).strip()[:300]
    if not body:
        raise ValueError("update carries no changes")
    url = f"{_ASANA_API}/tasks/{gid}?opt_fields=gid,name,permalink_url"
    return "PUT", url, {"data": body}, None


def _build_asana_comment(org_id: str, account_id: str, args: dict) -> tuple:
    gid = str(args.get("task") or args.get("task_gid") or "").strip()
    text = str(args.get("text") or args.get("body") or "").strip()
    if not gid.isdigit():
        raise ValueError("comment needs the task gid")
    if not text:
        raise ValueError("comment needs text")
    return "POST", f"{_ASANA_API}/tasks/{gid}/stories", {"data": {"text": text[:4000]}}, None


def _asana_receipt(action_type: str, resp_json: dict) -> tuple:
    data = (resp_json or {}).get("data") or {}
    gid = str(data.get("gid") or "")
    url = str(
        data.get("permalink_url")
        or (f"https://app.asana.com/0/0/{gid}/f" if gid else "")
    )
    kind = {
        "asana.create_task": "asana task",
        "asana.update_task": "asana task update",
        "asana.add_comment": "asana comment",
    }.get(action_type, "asana")
    return kind, url


# action_type -> (app_slug, builder, receipt_fn)
_MAPPER: dict[str, tuple[str, Builder, ReceiptFn]] = {
    "asana.create_task": ("asana", _build_asana_create, _asana_receipt),
    "asana.update_task": ("asana", _build_asana_update, _asana_receipt),
    "asana.add_comment": ("asana", _build_asana_comment, _asana_receipt),
}


# ── public surface (mirrors executor.py) ────────────────────────────────────

def enabled() -> bool:
    """The Pipedream execution plane is active only when its flag is on AND the
    Pipedream feature is configured."""
    return bool(settings.pipedream_executor) and pipedream_client.enabled()


def action_types() -> frozenset[str]:
    return frozenset(_MAPPER)


def _type_of(action: dict | None) -> str:
    return str((action or {}).get("type") or "").strip()


def handles(action: dict | None) -> bool:
    """True when this approved action executes through the Pipedream proxy."""
    return enabled() and _type_of(action) in _MAPPER


def _args_of(action: dict | None) -> dict:
    """Accept the runtime ``{type,args}`` shape and the nested executor shape
    (``{type, task|event|message}``)."""
    action = action if isinstance(action, dict) else {}
    if isinstance(action.get("args"), dict):
        return dict(action["args"])
    for key in ("task", "event", "message"):
        if isinstance(action.get(key), dict):
            return dict(action[key])
    return {
        k: v for k, v in action.items()
        if k not in {"type", "args", "task", "event", "message"}
    }


def execute_approved(org_id: str, action_id: str, action: dict) -> dict:
    """Execute one approved action through the Connect Proxy and settle its
    canonical ledger receipt. Never raises."""
    if not enabled():
        return {"ok": False, "skipped": "pipedream_executor off"}
    action_type = _type_of(action)
    spec = _MAPPER.get(action_type)
    if spec is None:
        return {"ok": False, "skipped": f"unhandled action type {action_type!r}"}
    org = str(org_id or "").strip()
    if not org:
        return {"ok": False, "error": "missing org"}

    app_slug, builder, receipt_fn = spec
    args = _args_of(action)

    # Resolve the org's connected account for this app (external_user_id = org).
    try:
        accounts = pipedream_client.list_accounts(org, app=app_slug)
    except pipedream_client.PipedreamError as exc:
        return _settle(action_id, org, False, action_type, "",
                       f"couldn't reach Pipedream ({type(exc).__name__})")
    account = next((a for a in accounts if a.get("id") and a.get("healthy", True)), None)
    if account is None:
        account = next((a for a in accounts if a.get("id")), None)
    if account is None:
        return _settle(action_id, org, False, action_type, "",
                       f"{app_slug} isn't connected in Pipedream")
    account_id = str(account["id"])

    try:
        method, url, body, headers = builder(org, account_id, args)
    except ValueError as exc:
        return _settle(action_id, org, False, action_type, "", str(exc))
    except Exception as exc:  # noqa: BLE001 — any builder fault ⇒ failed receipt
        return _settle(action_id, org, False, action_type, "",
                       f"bad arguments ({type(exc).__name__})")

    try:
        resp = pipedream_client.proxy_request(
            org, account_id, method, url, json_body=body, headers=headers,
        )
    except pipedream_client.PipedreamError as exc:
        return _settle(action_id, org, False, action_type, "",
                       f"proxy call failed ({type(exc).__name__})")
    if not resp.get("ok"):
        return _settle(action_id, org, False, action_type, "",
                       f"{app_slug} API returned {resp.get('status')}")

    kind, ref = receipt_fn(action_type, resp.get("json") or {})
    return _settle(action_id, org, True, action_type, ref, "", kind=kind)


def _settle(action_id: str, org: str, ok: bool, action_type: str, ref: str,
            error: str, *, kind: str = "") -> dict:
    """Write the canonical done/failed ledger receipt (route='pipedream') and
    mirror status to the Slack surface, exactly like executor.execute_approved.
    Returns the normalized result."""
    kind = kind or action_type or "action"
    result: dict[str, Any] = {"ok": ok, "kind": kind, "ref": ref}
    if not ok:
        result["error"] = error or "execution failed"

    aid = str(action_id or "").strip()
    if not aid:
        return result

    detail = ""
    try:
        if ok:
            detail = " · ".join(p for p in ("Pipedream", kind, ref) if p)[:300]
            ledger.set_action_status(
                aid, "done", detail, org_id=org,
                receipt={"kind": kind, "ref": ref, "route": "pipedream",
                         "runtime": "pipedream"},
            )
        else:
            detail = f"Pipedream · {error or 'failed'}"[:300]
            ledger.set_action_status(aid, "failed", detail, org_id=org)
    except Exception as exc:  # noqa: BLE001 — result still returns
        print(
            f"[pipedream_executor] status write skipped ({type(exc).__name__})",
            flush=True,
        )

    # Best-effort status projection to the Slack surface (never executes/routes).
    try:
        from .cedric import callback as slack_surface

        slack_surface.send_action_event(
            org, "action.status",
            {"action_id": aid, "status": "done" if ok else "failed",
             "detail": detail[:300], "receipt_url": ref if ok else ""},
        )
    except Exception:  # noqa: BLE001 — a UI mirror never breaks execution
        pass
    return result
