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

Scope (refined 2026-07-21): Pipedream owns Asana + the Google block (Gmail +
Calendar, Drive next) + the long tail; Slack stays on Cedric. Asana + the long
tail route to Pipedream unconditionally; the Google types ALSO have a native
adapter, so during the cutover they route to Pipedream only once the org has
connected that Google app in Pipedream, and fall back to the native token path
until then (executor.route_for_typed). All request bodies are the deterministic
mappers below — never model-constructed for a core-app write.
"""
from __future__ import annotations

import base64
import threading
import time
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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
    notes = str(args.get("notes") or "")
    # The Connect-Proxy path is a single request: carry subtasks/dependencies/
    # attachments in a structured notes tail so the fuller spec is never
    # silently dropped (the native adapter applies them as real objects).
    tail: list[str] = []
    subs = [str(x).strip() for x in (args.get("subtasks") or []) if str(x).strip()]
    if subs:
        tail.append("Subtasks: " + "; ".join(subs[:10]))
    deps = [str(x).strip() for x in (args.get("dependencies") or []) if str(x).strip()]
    if deps:
        tail.append("Depends on: " + "; ".join(deps[:10]))
    atts = [str(x).strip() for x in (args.get("attachments") or []) if str(x).strip()]
    if atts:
        tail.append("Attachments: " + " ".join(atts[:10]))
    if tail:
        notes = (notes + "\n\n" if notes else "") + "\n".join(tail)
    if notes:
        body["notes"] = notes[:4000]
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


# ── Google request builders (Gmail + Calendar, via the app's own REST API) ──
# The Pipedream account is connected per Google app (gmail / google_calendar /
# google_drive) with that app's scopes, so the proxy injects the right OAuth
# token. Arg shapes are IDENTICAL to the native adapters (google_client), so the
# brain's typing layer is unchanged — only the execution plane moves.
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_CAL_API = "https://www.googleapis.com/calendar/v3"
_DRIVE_API = "https://www.googleapis.com/drive/v3"


def _emails(value: Any) -> list[str]:
    """Normalize a str / comma-list / list into de-duped email strings."""
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value]
    else:
        parts = []
    out: list[str] = []
    for p in parts:
        if p and "@" in p and p not in out:
            out.append(p)
    return out


def _build_gmail_send(org_id: str, account_id: str, args: dict) -> tuple:
    to = _emails(args.get("to"))
    cc = _emails(args.get("cc"))
    bcc = _emails(args.get("bcc"))
    subject = str(args.get("subject") or "").strip()
    text = str(args.get("body") or args.get("text") or "")
    html = str(args.get("html_body") or args.get("html") or "")
    if not to:
        raise ValueError("email needs at least one recipient")
    if not subject and not text and not html:
        raise ValueError("email needs a subject or a body")
    if html:
        mime: Any = MIMEMultipart("alternative")
        mime.attach(MIMEText(text or "", "plain", _charset="utf-8"))
        mime.attach(MIMEText(html, "html", _charset="utf-8"))
    else:
        mime = MIMEText(text or "", _charset="utf-8")
    mime["To"] = ", ".join(to)
    mime["Subject"] = subject
    if cc:
        mime["Cc"] = ", ".join(cc)
    if bcc:
        mime["Bcc"] = ", ".join(bcc)
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    body: dict[str, Any] = {"raw": raw}
    thread_id = str(args.get("thread_id") or "").strip()
    if thread_id:
        body["threadId"] = thread_id
    return "POST", f"{_GMAIL_API}/messages/send", body, None


def _build_gmail_draft(org_id: str, account_id: str, args: dict) -> tuple:
    # Same MIME, but wrapped as a draft (nothing is sent).
    method, url, send_body, _ = _build_gmail_send(org_id, account_id, args)
    return "POST", f"{_GMAIL_API}/drafts", {"message": send_body}, None


def _gmail_receipt(action_type: str, resp_json: dict) -> tuple:
    if action_type == "gmail.create_draft":
        mid = str((resp_json.get("message") or {}).get("id") or resp_json.get("id") or "")
        return "email draft", (f"gmail:draft:{mid}" if mid else "")
    mid = str(resp_json.get("id") or "")
    return "email", (f"gmail:{mid}" if mid else "")


def _build_calendar_create(org_id: str, account_id: str, args: dict) -> tuple:
    summary = str(args.get("title") or args.get("summary") or "").strip()
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip()
    if not summary or not start or not end:
        raise ValueError("event needs a title, start and end")
    tz = str(args.get("timezone") or args.get("time_zone") or "UTC").strip() or "UTC"
    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": tz},
        "end": {"dateTime": end, "timeZone": tz},
        # Give the hold a real Meet join link, like the native adapter does.
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    if args.get("description"):
        body["description"] = str(args["description"])[:8000]
    attendees = _emails(args.get("attendees"))
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]
    url = (
        f"{_CAL_API}/calendars/primary/events"
        "?sendUpdates=all&conferenceDataVersion=1"
    )
    return "POST", url, body, None


def _build_calendar_update(org_id: str, account_id: str, args: dict) -> tuple:
    event_id = str(args.get("event_id") or args.get("event") or "").strip()
    if not event_id:
        raise ValueError("update needs the event id")
    body: dict[str, Any] = {}
    if args.get("title") or args.get("summary"):
        body["summary"] = str(args.get("title") or args.get("summary")).strip()
    tz = str(args.get("timezone") or args.get("time_zone") or "UTC").strip() or "UTC"
    if args.get("start"):
        body["start"] = {"dateTime": str(args["start"]).strip(), "timeZone": tz}
    if args.get("end"):
        body["end"] = {"dateTime": str(args["end"]).strip(), "timeZone": tz}
    if args.get("description"):
        body["description"] = str(args["description"])[:8000]
    if not body:
        raise ValueError("update carries no changes")
    url = f"{_CAL_API}/calendars/primary/events/{event_id}?sendUpdates=all"
    return "PATCH", url, body, None


def _calendar_receipt(action_type: str, resp_json: dict) -> tuple:
    ref = str(resp_json.get("htmlLink") or resp_json.get("id") or "")
    kind = "calendar event update" if action_type == "calendar.update_event" else "calendar event"
    return kind, ref


# action_type -> (app_slug, builder, receipt_fn)
_MAPPER: dict[str, tuple[str, Builder, ReceiptFn]] = {
    # Asana
    "asana.create_task": ("asana", _build_asana_create, _asana_receipt),
    "asana.update_task": ("asana", _build_asana_update, _asana_receipt),
    "asana.add_comment": ("asana", _build_asana_comment, _asana_receipt),
    # Gmail
    "email.send": ("gmail", _build_gmail_send, _gmail_receipt),
    "gmail.create_draft": ("gmail", _build_gmail_draft, _gmail_receipt),
    # Google Calendar
    "calendar.create_event": ("google_calendar", _build_calendar_create, _calendar_receipt),
    "calendar.update_event": ("google_calendar", _build_calendar_update, _calendar_receipt),
}

# The Google apps that ALSO have a native adapter — during the cutover these
# route to Pipedream only once the org has actually connected them there, and
# fall back to the native token path until then (see executor.route_for_typed).
_GOOGLE_APPS = frozenset({"gmail", "google_calendar", "google_drive"})


def app_for_type(action_type: str) -> str:
    """The Pipedream app slug that owns a mapped action type ('' if unmapped)."""
    spec = _MAPPER.get(str(action_type or "").strip())
    return spec[0] if spec else ""


def is_google_type(action_type: str) -> bool:
    """True for a mapped Gmail/Calendar/Drive action (has a native fallback)."""
    return app_for_type(action_type) in _GOOGLE_APPS


# ── generic pre-built actions (pd.<app>.run) ────────────────────────────────
# The long-tail plane: any Pipedream app the OWNER toggled on for an avatar can
# execute that app's own PRE-BUILT actions (run_action), instead of a hand-kept
# per-app request builder. The trade-off vs the deterministic mappers above:
# the model fills the action's props from meeting context, so the SAFETY moves
# to (a) the approval door — the card shows the exact action + props before a
# human clicks — and (b) the schema gate below: props are validated against the
# component's own configurable_props (unknown props dropped, required props
# checked, the auth prop NEVER model-writable) before anything runs.
import re as _re

_GENERIC_TYPE_RE = _re.compile(r"^pd\.([a-z0-9_][a-z0-9_-]{0,59})\.run$")


def generic_app(action_type: str | None) -> str:
    """The app slug of a generic ``pd.<app>.run`` action type, or ''."""
    m = _GENERIC_TYPE_RE.match(str(action_type or "").strip())
    return m.group(1) if m else ""


def _execute_generic(org: str, action_id: str, action_type: str,
                     app: str, args: dict) -> dict:
    """Run one pre-built Pipedream action with schema-gated props."""
    action_key = str(args.get("action_key") or "").strip()
    props = args.get("props") if isinstance(args.get("props"), dict) else {}
    if not action_key:
        return _settle(action_id, org, False, action_type, "",
                       "missing the Pipedream action key")
    # The component key embeds its app (github-create-issue) — a key from a
    # DIFFERENT app than the typed family would dodge the capability gate.
    if not action_key.startswith(app.replace("_", "-")) and not action_key.startswith(app):
        return _settle(action_id, org, False, action_type, "",
                       f"action {action_key!r} doesn't belong to {app}")

    try:
        accounts = pipedream_client.list_accounts(org, app=app)
    except pipedream_client.PipedreamError as exc:
        return _settle(action_id, org, False, action_type, "",
                       f"couldn't reach Pipedream ({type(exc).__name__})")
    account = next((a for a in accounts if a.get("id") and a.get("healthy", True)), None)
    if account is None:
        account = next((a for a in accounts if a.get("id")), None)
    if account is None:
        return _settle(action_id, org, False, action_type, "",
                       f"{app} isn't connected in Pipedream")
    account_id = str(account["id"])

    component = pipedream_client.get_component(action_key)
    schema = component.get("configurable_props") or []
    if not schema:
        # No schema ⇒ we can't validate what would run. Refuse rather than
        # fire a write we can't describe on the approval card.
        return _settle(action_id, org, False, action_type, "",
                       f"couldn't load the definition of {action_key}")

    auth_prop = ""
    allowed: dict[str, dict] = {}
    for p in schema:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        name = str(p["name"])
        if str(p.get("type") or "") == "app":
            # First app prop is the account slot. (Pre-built actions have one.)
            auth_prop = auth_prop or name
            continue
        allowed[name] = p
    if not auth_prop:
        return _settle(action_id, org, False, action_type, "",
                       f"{action_key} has no account slot to fill")

    # Schema gate: keep only props the component declares; the model can never
    # smuggle an extra field (least of all the auth prop) into the run.
    configured: dict = {
        name: value for name, value in props.items()
        if name in allowed and name != auth_prop
    }
    missing = [
        name for name, p in allowed.items()
        if not p.get("optional") and not p.get("hidden")
        and configured.get(name) in (None, "", [], {})
    ]
    if missing:
        return _settle(action_id, org, False, action_type, "",
                       "missing required fields: " + ", ".join(sorted(missing)[:6]))
    configured[auth_prop] = {"authProvisionId": account_id}

    try:
        result = pipedream_client.run_action(org, action_key, configured)
    except pipedream_client.PipedreamError as exc:
        # Plan gate ≠ outage: pre-built actions ("tool calling") sit on a
        # higher Pipedream tier than Connect accounts + proxy. Say so on the
        # receipt — the typed Asana/Gmail/Calendar path is unaffected.
        if "current plan" in str(exc).lower():
            return _settle(
                action_id, org, False, action_type, "",
                f"{app} pre-built actions need Pipedream's tool-calling tier "
                "(pipedream.com/pricing). Core Asana/Gmail/Calendar actions "
                "run via the Connect proxy and are unaffected.",
            )
        return _settle(action_id, org, False, action_type, "",
                       f"{app} action failed ({type(exc).__name__})")
    exports = result.get("exports") if isinstance(result.get("exports"), dict) else {}
    ref = str(exports.get("$summary") or "").strip()[:300]
    kind = f"{app} · {component.get('name') or action_key}"[:120]
    return _settle(action_id, org, True, action_type, ref, "", kind=kind)

# ── public surface (mirrors executor.py) ────────────────────────────────────

def enabled() -> bool:
    """The Pipedream execution plane is active only when its flag is on AND the
    Pipedream feature is configured."""
    return bool(settings.pipedream_executor) and pipedream_client.enabled()


def action_types() -> frozenset[str]:
    return frozenset(_MAPPER)


# ── connection probe (for the availability gates) ───────────────────────────
# Cached best-effort: lets a Pipedream-only connection count as "connected" so
# the native connection can be dropped without silencing the avatar's tool.
_CONN_TTL_S = 120.0
_conn_cache: dict[tuple[str, str], tuple[bool, float]] = {}
_conn_lock = threading.Lock()


def app_connected(org_id: str, app_slug: str) -> bool:
    """Best-effort: does this org have a connected account for ``app_slug`` in
    Pipedream? Cached ~2 min. False when the executor is off or Pipedream is
    unreachable (a transient failure is not cached, so it retries next time)."""
    if not enabled():
        return False
    org = str(org_id or "").strip()
    app = str(app_slug or "").strip().lower()
    if not (org and app):
        return False
    key = (org, app)
    now = time.monotonic()
    with _conn_lock:
        hit = _conn_cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
    try:
        accounts = pipedream_client.list_accounts(org, app=app)
        ok = any(a.get("id") for a in accounts)
    except pipedream_client.PipedreamError:
        return False  # transient — don't cache, retry next call
    with _conn_lock:
        _conn_cache[key] = (ok, now + _CONN_TTL_S)
    return ok


def read_calendar_events(org_id: str, *, max_results: int = 8) -> dict:
    """READ-ONLY upcoming events via the Connect proxy (google_calendar) — the
    calendar-brief FALLBACK for orgs whose Google lives in Pipedream and who
    never did the native OAuth. Native stays first in line (google_client);
    this runs only when that path has no token. Same ``{"ok", "events"}``
    contract as ``google_client.list_calendar_events`` (raw Google items);
    never raises. Live gap 2026-07-22: the join-time brief was native-only,
    so a Pipedream-Google org's avatar had NO calendar sight and improvised
    'I'll check it' promises seven times in one call."""
    if not (enabled() and pipedream_client.enabled()):
        return {"ok": False, "error": "pipedream off"}
    org = str(org_id or "").strip()
    if not org:
        return {"ok": False, "error": "no org"}
    try:
        accounts = pipedream_client.list_accounts(org, app="google_calendar")
    except pipedream_client.PipedreamError as exc:
        return {"ok": False, "error": f"pipedream accounts ({type(exc).__name__})"}
    account = next(
        (a for a in accounts if a.get("id") and a.get("healthy", True)), None
    ) or next((a for a in accounts if a.get("id")), None)
    if account is None:
        return {"ok": False, "error": "google_calendar not connected in Pipedream"}
    from datetime import datetime, timezone as _tz

    now_iso = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        n = max(1, min(int(max_results or 8), 50))
    except (TypeError, ValueError):
        n = 8
    url = (
        "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        f"?timeMin={now_iso}&maxResults={n}&singleEvents=true&orderBy=startTime"
    )
    resp = pipedream_client.proxy_request(org, str(account["id"]), "GET", url)
    if not resp.get("ok"):
        return {"ok": False, "error": str(resp.get("error") or "proxy read failed")}
    items = (resp.get("json") or {}).get("items") or []
    return {"ok": True, "events": [i for i in items if isinstance(i, dict)]}


def note_connections(org_id: str, connected_slugs: set[str] | frozenset[str]) -> None:
    """Write-through from a FRESH accounts listing (the Connections view):
    mark these slugs connected NOW so the avatar cards and route probes stop
    serving a stale negative for up to _CONN_TTL_S (live 2026-07-22: after a
    disconnect+reconnect the avatar card kept saying "Connect in Connections"
    for two minutes). Only positives are asserted — absence in one filtered
    listing is not proof of disconnection."""
    org = str(org_id or "").strip()
    if not org:
        return
    now = time.monotonic()
    with _conn_lock:
        for slug in connected_slugs:
            s = str(slug or "").strip().lower()
            if s:
                _conn_cache[(org, s)] = (True, now + _CONN_TTL_S)


def forget_connection(org_id: str, app_slug: str) -> None:
    """Immediate cache bust after an explicit disconnect — the next probe
    re-reads Pipedream instead of serving a stale True."""
    org = str(org_id or "").strip()
    app = str(app_slug or "").strip().lower()
    if org and app:
        with _conn_lock:
            _conn_cache.pop((org, app), None)


def _reset_conn_cache() -> None:
    """Test seam."""
    with _conn_lock:
        _conn_cache.clear()


def _type_of(action: dict | None) -> str:
    return str((action or {}).get("type") or "").strip()


def handles(action: dict | None) -> bool:
    """True when this approved action executes through the Pipedream proxy —
    a hand-mapped type (Asana) or a generic pre-built one (pd.<app>.run)."""
    if not enabled():
        return False
    t = _type_of(action)
    return t in _MAPPER or bool(generic_app(t))


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
    org = str(org_id or "").strip()
    if not org:
        return {"ok": False, "error": "missing org"}
    app = generic_app(action_type)
    if app and action_type not in _MAPPER:
        return _execute_generic(org, action_id, action_type, app, _args_of(action))
    spec = _MAPPER.get(action_type)
    if spec is None:
        return {"ok": False, "skipped": f"unhandled action type {action_type!r}"}

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
    # Phase 1 read-back verify (owner 2026-07-22): re-read the object we just
    # wrote so the receipt is EVIDENCE, not presumption. Best-effort — a
    # verify hiccup never fails a succeeded action.
    verified = _verify_written(org, account_id, action_type,
                               resp.get("json") or {})
    return _settle(action_id, org, True, action_type, ref, "",
                   kind=(kind + " · verified") if verified else kind)


# Read-back endpoints per family: GET the object by the id the CREATE/SEND
# response returned. Any missing id / non-2xx / exception ⇒ not verified.
def _verify_written(org: str, account_id: str, action_type: str,
                    resp_json: dict) -> bool:
    try:
        data = resp_json.get("data") if isinstance(resp_json.get("data"), dict) \
            else resp_json
        if action_type.startswith("asana."):
            gid = str((data or {}).get("gid") or "")
            if not gid:
                return False
            check = pipedream_client.proxy_request(
                org, account_id, "GET",
                f"{_ASANA_API}/tasks/{gid}?opt_fields=gid",
            )
            return bool(check.get("ok"))
        if action_type in ("calendar.create_event", "calendar.update_event"):
            eid = str((data or {}).get("id") or "")
            if not eid:
                return False
            check = pipedream_client.proxy_request(
                org, account_id, "GET",
                f"{_CAL_API}/calendars/primary/events/{eid}",
            )
            return bool(check.get("ok"))
        if action_type == "email.send":
            mid = str((data or {}).get("id") or "")
            if not mid:
                return False
            check = pipedream_client.proxy_request(
                org, account_id, "GET",
                f"{_GMAIL_API}/messages/{mid}?format=minimal",
            )
            return bool(check.get("ok"))
        if action_type == "gmail.create_draft":
            did = str((data or {}).get("id") or "")
            if not did:
                return False
            check = pipedream_client.proxy_request(
                org, account_id, "GET", f"{_GMAIL_API}/drafts/{did}",
            )
            return bool(check.get("ok"))
    except Exception:  # noqa: BLE001 — verification is optional evidence
        return False
    return False


def dry_run(org_id: str, action: dict) -> dict:
    """Run a mapped action through the proxy WITHOUT touching the ledger — for
    the dashboard "test integrations" tool. Same account-resolution + builder +
    proxy as execute_approved; returns {ok, kind, ref, route, error?, status?}.
    Never raises."""
    if not enabled():
        return {"ok": False, "error": "pipedream executor is off"}
    action_type = _type_of(action)
    spec = _MAPPER.get(action_type)
    if spec is None:
        return {"ok": False, "error": f"unhandled action type {action_type!r}"}
    org = str(org_id or "").strip()
    if not org:
        return {"ok": False, "error": "missing org"}
    app_slug, builder, receipt_fn = spec
    args = _args_of(action)
    try:
        accounts = pipedream_client.list_accounts(org, app=app_slug)
    except pipedream_client.PipedreamError as exc:
        return {"ok": False, "error": f"couldn't reach Pipedream ({type(exc).__name__})"}
    account = next((a for a in accounts if a.get("id") and a.get("healthy", True)), None)
    if account is None:
        account = next((a for a in accounts if a.get("id")), None)
    if account is None:
        return {"ok": False, "error": f"{app_slug} isn't connected in Pipedream"}
    account_id = str(account["id"])
    try:
        method, url, body, headers = builder(org, account_id, args)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"bad arguments ({type(exc).__name__})"}
    try:
        resp = pipedream_client.proxy_request(
            org, account_id, method, url, json_body=body, headers=headers)
    except pipedream_client.PipedreamError as exc:
        return {"ok": False, "error": f"proxy call failed ({type(exc).__name__})"}
    if not resp.get("ok"):
        return {"ok": False, "status": resp.get("status"),
                "error": f"{app_slug} API returned {resp.get('status')}"}
    kind, ref = receipt_fn(action_type, resp.get("json") or {})
    return {"ok": True, "kind": kind, "ref": ref, "route": "pipedream"}


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
