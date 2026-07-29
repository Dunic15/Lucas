"""Declarative registry of connected-app API surfaces.

**Adding an app is adding a row here** (or one JSON entry in
``CONNECTED_APP_REGISTRY_EXTRA``) — never new code. That is the same principle
as "adding an avatar is adding a folder": every per-vendor quirk that used to be
an ``if app == "notion"`` branch inside ``pipedream_executor`` is DATA in this
table (the version header, the POST endpoints that are semantically reads), so a
newly connected app reaches OpenClaw Chat, the meeting tool brief and the proxy
executor through one path with no vendor-specific code anywhere.

SSRF boundary — this module is the ONLY place an outbound API host can be
authorised. We never derive a host from an app slug: a connected app with no
registered host simply has no generic proxy path (``app_policy`` reports that
limit honestly instead of inventing one). Host *patterns* are allowed only in
the ``*.suffix`` form, so a wildcard can never widen past a registrable domain
the vendor controls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from fnmatch import fnmatchcase

from ..config import settings

# Methods the Connect Proxy may ever carry. Anything else is refused before a
# request is built, so an exotic verb can't be smuggled through the planner.
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
# Methods that are reads by definition. Everything else is a write unless the
# app's own ``read_ops`` marks that one endpoint as a read (search/query APIs).
READ_METHODS = frozenset({"GET", "HEAD"})
# Request headers a model may set. Authorization is never in this set —
# credentials are injected server-side by Pipedream Connect, never by us.
SAFE_HEADERS = frozenset(
    {"accept", "content-type", "notion-version", "consistencylevel"}
)


@dataclass(frozen=True)
class AppSpec:
    """One connected app's API surface, as data."""

    slug: str
    label: str
    hosts: frozenset[str]
    # Host patterns for multi-tenant vendors (``*.atlassian.net``). Only the
    # ``*.suffix`` form is accepted — see ``_valid_pattern``.
    host_patterns: tuple[str, ...] = ()
    # Short "how to drive this API" lines handed to the planner. Untrusted
    # display text: they name endpoints, they are never instructions.
    guides: tuple[str, ...] = ()
    # Vendor headers this API requires on every call (e.g. Notion-Version).
    headers: tuple[tuple[str, str], ...] = ()
    # Non-GET operations that are semantically READS, as "METHOD /path" globs.
    # This is what lets the planner inspect a search API during planning
    # without that inspection counting as a side effect.
    read_ops: tuple[str, ...] = ()
    # Operations this app never allows, as "METHOD /path" globs.
    deny_ops: tuple[str, ...] = ()
    # Risk floor for writes on this app; the policy may raise it, never lower it.
    write_risk: str = "high"
    # A real capability limit, surfaced verbatim to the planner so it explains
    # the boundary instead of inventing an execution.
    note: str = ""

    def default_headers(self) -> dict[str, str]:
        return {name: value for name, value in self.headers}


def _spec(slug: str, label: str, hosts: tuple[str, ...], **kw) -> AppSpec:
    return AppSpec(slug=slug, label=label, hosts=frozenset(hosts), **kw)


# ── the table ───────────────────────────────────────────────────────────────
# Ordered by slug. `guides` stay short: they ride in the planner prompt.
_APPS: dict[str, AppSpec] = {
    app.slug: app
    for app in (
        _spec(
            "airtable", "Airtable", ("api.airtable.com",),
            guides=(
                "List/create records: GET or POST https://api.airtable.com/v0/{baseId}/{tableIdOrName}",
                "Update a record: PATCH https://api.airtable.com/v0/{baseId}/{tableIdOrName}/{recordId}",
            ),
        ),
        _spec(
            "asana", "Asana", ("app.asana.com",),
            guides=(
                "List/create tasks: GET or POST https://app.asana.com/api/1.0/tasks",
                "Update task: PUT https://app.asana.com/api/1.0/tasks/{taskGid}",
                "Comment: POST https://app.asana.com/api/1.0/tasks/{taskGid}/stories",
            ),
        ),
        _spec(
            "calendly", "Calendly", ("api.calendly.com",),
            guides=(
                "List scheduled events: GET https://api.calendly.com/scheduled_events",
            ),
        ),
        _spec(
            "clickup", "ClickUp", ("api.clickup.com",),
            guides=(
                "List/create tasks: GET or POST https://api.clickup.com/api/v2/list/{listId}/task",
                "Update task: PUT https://api.clickup.com/api/v2/task/{taskId}",
            ),
        ),
        _spec(
            "dropbox", "Dropbox",
            ("api.dropboxapi.com", "content.dropboxapi.com"),
            read_ops=(
                "POST /2/files/list_folder",
                "POST /2/files/list_folder/continue",
                "POST /2/files/search_v2",
                "POST /2/files/get_metadata",
            ),
            guides=(
                "List a folder: POST https://api.dropboxapi.com/2/files/list_folder",
                "Create a folder: POST https://api.dropboxapi.com/2/files/create_folder_v2",
            ),
        ),
        _spec(
            "github", "GitHub", ("api.github.com",),
            guides=(
                "List/create issues: GET or POST https://api.github.com/repos/{owner}/{repo}/issues",
                "Update issue: PATCH https://api.github.com/repos/{owner}/{repo}/issues/{number}",
                "Create comment: POST https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
            ),
        ),
        _spec(
            "gmail", "Gmail", ("gmail.googleapis.com",),
            guides=(
                "Search messages: GET https://gmail.googleapis.com/gmail/v1/users/me/messages?q={query}",
                "Create draft: POST https://gmail.googleapis.com/gmail/v1/users/me/drafts",
                "Send MIME message: POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            ),
        ),
        _spec(
            "google_calendar", "Google Calendar", ("www.googleapis.com",),
            guides=(
                "List/create events: GET or POST https://www.googleapis.com/calendar/v3/calendars/primary/events",
                "Update event: PATCH https://www.googleapis.com/calendar/v3/calendars/primary/events/{eventId}",
            ),
        ),
        _spec(
            "google_drive", "Google Drive", ("www.googleapis.com",),
            guides=(
                "Search/create files: GET or POST https://www.googleapis.com/drive/v3/files",
                "Update file metadata: PATCH https://www.googleapis.com/drive/v3/files/{fileId}",
                "Share file: POST https://www.googleapis.com/drive/v3/files/{fileId}/permissions",
            ),
        ),
        _spec(
            "hubspot", "HubSpot", ("api.hubapi.com",),
            read_ops=("POST /crm/v3/objects/*/search",),
            guides=(
                "List/create CRM objects: GET or POST https://api.hubapi.com/crm/v3/objects/{objectType}",
                "Search objects: POST https://api.hubapi.com/crm/v3/objects/{objectType}/search",
                "Update CRM object: PATCH https://api.hubapi.com/crm/v3/objects/{objectType}/{objectId}",
            ),
        ),
        _spec(
            "intercom", "Intercom", ("api.intercom.io",),
            read_ops=("POST /contacts/search", "POST /conversations/search"),
            guides=(
                "Search contacts: POST https://api.intercom.io/contacts/search",
            ),
        ),
        _spec(
            "jira", "Jira", (),
            host_patterns=("*.atlassian.net",),
            read_ops=("POST /rest/api/*/search",),
            guides=(
                "Search issues: POST https://{site}.atlassian.net/rest/api/3/search",
                "Create issue: POST https://{site}.atlassian.net/rest/api/3/issue",
                "Update issue: PUT https://{site}.atlassian.net/rest/api/3/issue/{issueIdOrKey}",
            ),
        ),
        _spec(
            "linear", "Linear", ("api.linear.app",),
            guides=("Queries and mutations: POST https://api.linear.app/graphql",),
            note=(
                "Linear exposes one GraphQL endpoint, so a read and a write are "
                "the same HTTP operation. Planner reads are therefore not "
                "available for Linear: resolve IDs with the user, or use a "
                "deterministic action."
            ),
        ),
        _spec(
            "microsoft_teams", "Microsoft Teams", ("graph.microsoft.com",),
            guides=(
                "List joined teams: GET https://graph.microsoft.com/v1.0/me/joinedTeams",
                "Post a channel message: POST https://graph.microsoft.com/v1.0/teams/{teamId}/channels/{channelId}/messages",
            ),
        ),
        _spec(
            "monday", "monday.com", ("api.monday.com",),
            guides=("Queries and mutations: POST https://api.monday.com/v2",),
            note=(
                "monday.com exposes one GraphQL endpoint, so a read and a write "
                "are the same HTTP operation. Planner reads are not available."
            ),
        ),
        _spec(
            "notion", "Notion", ("api.notion.com",),
            headers=(("Notion-Version", "2026-03-11"),),
            read_ops=(
                "POST /v1/search",
                "POST /v1/data_sources/*/query",
                "POST /v1/databases/*/query",
            ),
            guides=(
                "Search shared content: POST https://api.notion.com/v1/search",
                "Create private page: POST https://api.notion.com/v1/pages with parent {type:'workspace',workspace:true}",
                "Update page: PATCH https://api.notion.com/v1/pages/{pageId}",
                "Append blocks: PATCH https://api.notion.com/v1/blocks/{blockId}/children",
            ),
        ),
        _spec(
            "outlook", "Outlook", ("graph.microsoft.com",),
            guides=(
                "List messages: GET https://graph.microsoft.com/v1.0/me/messages",
                "Send mail: POST https://graph.microsoft.com/v1.0/me/sendMail",
            ),
        ),
        _spec(
            "pipedrive", "Pipedrive", (),
            host_patterns=("*.pipedrive.com",),
            guides=(
                "List/create deals: GET or POST https://{company}.pipedrive.com/api/v1/deals",
            ),
        ),
        _spec(
            "slack", "Slack", ("slack.com",),
            guides=(
                "Post message: POST https://slack.com/api/chat.postMessage",
                "List channel history: GET https://slack.com/api/conversations.history?channel={channelId}",
            ),
        ),
        _spec(
            "stripe", "Stripe", ("api.stripe.com",),
            # Money moves: deletes are never model-constructed here.
            deny_ops=("DELETE *",),
            guides=(
                "List customers: GET https://api.stripe.com/v1/customers",
                "Create a customer: POST https://api.stripe.com/v1/customers",
            ),
        ),
        _spec(
            "todoist", "Todoist", ("api.todoist.com",),
            guides=(
                "List/create tasks: GET or POST https://api.todoist.com/rest/v2/tasks",
                "Update task: POST https://api.todoist.com/rest/v2/tasks/{taskId}",
            ),
        ),
        _spec(
            "trello", "Trello", ("api.trello.com",),
            guides=(
                "List/create cards: GET or POST https://api.trello.com/1/cards",
                "Update card: PUT https://api.trello.com/1/cards/{cardId}",
            ),
        ),
        _spec(
            "zendesk", "Zendesk", (),
            host_patterns=("*.zendesk.com",),
            guides=(
                "Search tickets: GET https://{subdomain}.zendesk.com/api/v2/search.json?query={q}",
                "Create ticket: POST https://{subdomain}.zendesk.com/api/v2/tickets.json",
            ),
        ),
    )
}


def _valid_pattern(pattern: str) -> bool:
    """A wildcard host must be ``*.registrable.tld`` — never ``*suffix``.

    Without the mandatory dot, ``*.evil.com`` style typos become
    ``*evil.com``, which would also match ``notevil.com``. This is the whole
    reason patterns are validated rather than trusted.
    """
    value = str(pattern or "").strip().lower()
    if not value.startswith("*."):
        return False
    rest = value[2:]
    return bool(rest) and "*" not in rest and "." in rest


def _extra_specs() -> dict[str, AppSpec]:
    """Operator-supplied rows from ``CONNECTED_APP_REGISTRY_EXTRA`` (JSON).

    Shape: ``{"<slug>": {"label", "hosts": [...], "host_patterns": [...],
    "guides": [...], "headers": {..}, "read_ops": [...], "deny_ops": [...],
    "note": "..."}}``. A malformed blob is ignored wholesale — a bad env var
    must never take the connected-app catalog down.
    """
    raw = (getattr(settings, "connected_app_registry_extra", "") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, AppSpec] = {}
    for slug, value in list(data.items())[:100]:
        name = str(slug or "").strip().lower()
        if not name or not isinstance(value, dict):
            continue
        hosts = frozenset(
            str(h).strip().lower()
            for h in (value.get("hosts") or [])
            if str(h or "").strip() and "*" not in str(h)
        )
        patterns = tuple(
            str(p).strip().lower()
            for p in (value.get("host_patterns") or [])
            if _valid_pattern(str(p))
        )
        if not hosts and not patterns:
            continue
        headers = value.get("headers")
        out[name] = AppSpec(
            slug=name,
            label=str(value.get("label") or name.replace("_", " ").title())[:80],
            hosts=hosts,
            host_patterns=patterns,
            guides=tuple(str(g)[:300] for g in (value.get("guides") or [])[:8]),
            headers=tuple(
                (str(k)[:60], str(v)[:200])
                for k, v in (headers.items() if isinstance(headers, dict) else [])
            ),
            read_ops=tuple(str(o)[:200] for o in (value.get("read_ops") or [])[:20]),
            deny_ops=tuple(str(o)[:200] for o in (value.get("deny_ops") or [])[:20]),
            note=str(value.get("note") or "")[:400],
        )
    return out


def all_specs() -> dict[str, AppSpec]:
    """Built-in rows plus operator extras (extras win on a slug clash)."""
    return {**_APPS, **_extra_specs()}


def spec_for(app_slug: str) -> AppSpec | None:
    """The registered API surface for one app slug, or ``None``."""
    return all_specs().get(str(app_slug or "").strip().lower())


def registered_slugs() -> frozenset[str]:
    return frozenset(all_specs())


def api_hosts() -> dict[str, list[str]]:
    """Public, immutable view of every app's approved hosts (exact + pattern)."""
    return {
        slug: sorted(spec.hosts) + sorted(spec.host_patterns)
        for slug, spec in sorted(all_specs().items())
    }


def api_guides() -> dict[str, list[str]]:
    return {
        slug: list(spec.guides)
        for slug, spec in sorted(all_specs().items())
        if spec.guides
    }


def host_allowed(spec: AppSpec | None, host: str) -> bool:
    """SSRF gate: is ``host`` an approved API host for this app?"""
    if spec is None:
        return False
    name = str(host or "").strip().lower().rstrip(".")
    if not name:
        return False
    if name in spec.hosts:
        return True
    return any(
        _valid_pattern(pattern) and fnmatchcase(name, pattern)
        for pattern in spec.host_patterns
    )


def operation_matches(patterns: tuple[str, ...], method: str, path: str) -> bool:
    """``"POST /v1/search"``-style glob match against one operation."""
    op = f"{str(method or '').strip().upper()} {str(path or '') or '/'}"
    return any(fnmatchcase(op, str(p).strip()) for p in patterns if str(p).strip())


def is_read_operation(spec: AppSpec | None, method: str, path: str) -> bool:
    """GET/HEAD, or a POST the app itself declares as a search/query read."""
    verb = str(method or "").strip().upper()
    if verb in READ_METHODS:
        return True
    if spec is None:
        return False
    return operation_matches(spec.read_ops, verb, path)
