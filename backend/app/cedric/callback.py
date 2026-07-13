"""Callbacks to the orchestrator (Cedric) that booked a session.

A session started with a `callback_url` gets three kinds of events POSTed back
(see the Cedric X Laura project's docs/04-api-contract.md):

  - session.status    — best-effort, single attempt (joining / live / failed).
  - session.ended     — the full artifact; retried with backoff because losing
                        it means the orchestrator has to fall back to polling.
  - action.requested  — someone asked the avatar to DO something mid-meeting
                        (tools.queue_action); best-effort, single attempt —
                        the artifact's actions[] is the authoritative list.

Requests are signed with `X-Laura-Signature: t=<unix_ts>,v1=<hmac_sha256_hex>`
over `t + "." + raw_body` using LAURA_WEBHOOK_SECRET (Slack/Stripe-style), and
carry a per-workspace bearer for customer orgs. The deployment-level
`LAURA_WEBHOOK_TOKEN` remains only for the Demo/legacy service path.

Everything here is best-effort by design: a callback failure must NEVER block
or fail the meeting lifecycle (finalize already saved the artifact — the
orchestrator can always poll GET /sessions/{bot_id}/artifact). All functions
are sync (called via run_in_threadpool / background tasks).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..config import settings
from . import secret_registry

# Retry schedule for session.ended (seconds between attempts). Module-level so
# tests can monkeypatch it to zeros.
ENDED_BACKOFF: tuple[float, ...] = (5.0, 25.0, 120.0)


def _secret_for(org_id: str) -> str:
    """The signing secret for an org: its entry in the per-client registry
    (LAURA_WEBHOOK_SECRETS_BY_ORG, a JSON object {org_id: secret}) when
    present, else the global LAURA_WEBHOOK_SECRET. The registry is how each
    connected workspace gets its own credential (minted by the orchestrator's
    /api/laura/orgs provisioning) without rotating anyone else's."""
    per_org = secret_registry.secret_for(org_id)
    if per_org:
        return per_org
    return settings.laura_webhook_secret.strip()


@dataclass(frozen=True, repr=False)
class ProvisionResult:
    """Provision response kept in memory only; repr deliberately redacts it."""

    status_code: int
    webhook_secret: str = ""
    webhook_token: str = ""

    def __bool__(self) -> bool:
        return 200 <= self.status_code < 300

    def __repr__(self) -> str:
        return (
            f"ProvisionResult(status_code={self.status_code}, "
            f"webhook_secret={'<redacted>' if self.webhook_secret else '<missing>'}, "
            f"webhook_token={'<redacted>' if self.webhook_token else '<missing>'})"
        )


def _bearer_for(org_id: str, legacy: str = "") -> str:
    """Bearer for Laura→Cedric calls.

    Real customer orgs must use Cedric's per-workspace token; the deployment
    token is retained only for empty/Demo bootstrap and legacy traffic.
    """
    org = (org_id or "").strip()
    if org and org != settings.demo_org_id:
        return secret_registry.bearer_for(org)
    return (legacy or "").strip()


def _signature_headers(body: bytes, org_id: str = "") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = _bearer_for(org_id, settings.laura_webhook_token)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    secret = _secret_for(org_id)
    if secret:
        ts = str(int(time.time()))
        mac = hmac.new(
            secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
        ).hexdigest()
        headers["X-Laura-Signature"] = f"t={ts},v1={mac}"
    return headers


# Redirect statuses that preserve the request method (plus 301, which most
# hosts use interchangeably with 308 for apex→www).
_REDIRECTS = (301, 307, 308)


def _redirect_target(resp: httpx.Response) -> str | None:
    loc = resp.headers.get("location")
    return str(resp.url.join(loc)) if resp.status_code in _REDIRECTS and loc else None


def _post(url: str, payload: dict) -> httpx.Response:
    body = json.dumps(payload).encode()
    org_id = str(payload.get("org_id") or "")
    # A redirect (e.g. Vercel apex→www) is followed manually for one hop:
    # httpx's follow_redirects strips Authorization when the host changes, so
    # the auth + signature headers must be re-applied to the new URL.
    with httpx.Client(timeout=settings.callback_timeout_seconds) as client:
        resp = client.post(url, content=body, headers=_signature_headers(body, org_id))
        target = _redirect_target(resp)
        if target:
            resp = client.post(target, content=body, headers=_signature_headers(body, org_id))
        return resp


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def send_status(
    integration: dict | None, bot_id: str, status: str, detail: str = ""
) -> bool:
    """POST a session.status event. One attempt; the orchestrator's watchdog is
    the backstop for a missed status. Returns True when delivered (2xx)."""
    url = (integration or {}).get("callback_url") or ""
    if not url:
        return False
    payload = {
        "event": "session.status",
        "bot_id": bot_id,
        "org_id": (integration or {}).get("org_id") or "",
        "external_ref": (integration or {}).get("external_ref") or {},
        "status": status,
        "detail": detail,
        "at": _now_iso(),
    }
    try:
        resp = _post(url, payload)
        return 200 <= resp.status_code < 300
    except Exception as e:  # noqa: BLE001 — never let a callback break the call
        print(f"[cedric-callback] status '{status}' delivery failed: {e}", flush=True)
        return False


def send_action_requested(integration: dict | None, bot_id: str, item: dict) -> bool:
    """POST an action.requested event the moment the avatar queues an action
    request live (tools.queue_action), so the orchestrator's approval card is
    ready before the meeting ends. Same discipline as session.status: single
    attempt, best-effort — the artifact's actions[] in session.ended is the
    authoritative, complete list. PII rule: only the distilled action text /
    owner / due ever leave — never transcript content."""
    url = (integration or {}).get("callback_url") or ""
    if not url:
        return False
    payload = {
        "event": "action.requested",
        "bot_id": bot_id,
        # Stable per-action id: the same value appears on this action inside the
        # later session.ended artifact, so the orchestrator matches its live
        # approval card to the final action (and dedupes) on the id — and passes
        # it back to POST /org/actions/{action_id}/resolve to close the loop.
        "action_id": (item or {}).get("action_id", ""),
        "org_id": (integration or {}).get("org_id") or "",
        "external_ref": (integration or {}).get("external_ref") or {},
        "action": (item or {}).get("action", ""),
        "owner": (item or {}).get("owner", ""),
        "due": (item or {}).get("due", ""),
        "at": _now_iso(),
    }
    try:
        resp = _post(url, payload)
        ok = 200 <= resp.status_code < 300
        # PII-safe telemetry (action_id + HTTP status only, never the action
        # text): a live "Cedric said he could but did nothing" report is
        # undiagnosable otherwise — a non-2xx from the surface used to be
        # swallowed here (returned False silently, no log).
        print(
            "[cedric-callback] action.requested "
            f"{'delivered' if ok else 'REJECTED by surface'} "
            f"(action_id={payload['action_id']!r}, HTTP {resp.status_code})",
            flush=True,
        )
        return ok
    except Exception as e:  # noqa: BLE001 — never let a callback break the call
        print(f"[cedric-callback] action.requested delivery failed: {e}", flush=True)
        return False


def send_ended(integration: dict | None, bot_id: str, artifact: dict) -> bool:
    """POST the session.ended event with the artifact. Retries on any failure
    (ENDED_BACKOFF schedule); gives up after the last attempt — the artifact
    stays available at GET /sessions/{bot_id}/artifact for polling."""
    url = (integration or {}).get("callback_url") or ""
    if not url:
        return False
    payload = {
        "event": "session.ended",
        "bot_id": bot_id,
        "org_id": (integration or {}).get("org_id") or "",
        "external_ref": (integration or {}).get("external_ref") or {},
        "ended_at": _now_iso(),
        # Belt and braces: callers pass the wire copy already, but raw
        # transcripts are PII and must never leave regardless of the caller.
        "artifact": {k: v for k, v in artifact.items() if k != "transcript"},
    }
    attempts = len(ENDED_BACKOFF) + 1
    for attempt in range(attempts):
        try:
            resp = _post(url, payload)
            if 200 <= resp.status_code < 300:
                # Confirms the surface actually ACCEPTED the artifact — the
                # [finalize] orchestrated=True flag only means a callback_url
                # was set + the POST was fired, not that Cedric received it.
                print(
                    f"[cedric-callback] session.ended delivered (HTTP "
                    f"{resp.status_code})",
                    flush=True,
                )
                return True
            reason: str = f"HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            reason = str(e)
        if attempt < len(ENDED_BACKOFF):
            print(
                f"[cedric-callback] session.ended attempt {attempt + 1} failed "
                f"({reason}); retrying",
                flush=True,
            )
            time.sleep(ENDED_BACKOFF[attempt])
    print(
        f"[cedric-callback] session.ended delivery gave up after {attempts} "
        f"attempts ({reason}); orchestrator must poll the artifact",
        flush=True,
    )
    return False


def _context_request_url(integration: dict | None) -> str:
    """The context GET URL, with the routing hints Cedric's endpoint needs to
    locate the brief appended as query params: ``external_ref.team`` ->
    ``team``, ``external_ref.slack_channel`` -> ``channel`` (his side is
    "No team => empty brief", so a bare URL comes back empty). Query params
    already present in the configured URL are preserved, and an explicit
    ``team``/``channel`` there wins over external_ref (never duplicated).
    Routing metadata only — transcript content never rides this URL."""
    url = (integration or {}).get("context_url") or ""
    ref = (integration or {}).get("external_ref") or {}
    if not url or not isinstance(ref, dict):
        return url
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    configured = {k for k, _ in query}
    added = False
    for ref_key, param in (("team", "team"), ("slack_channel", "channel")):
        value = str(ref.get(ref_key) or "").strip()
        if value and param not in configured:
            query.append((param, value))
            added = True
    if not added:
        return url  # nothing to append: keep the configured URL byte-for-byte
    return urlunsplit(parts._replace(query=urlencode(query)))


def fetch_context(integration: dict | None) -> dict | None:
    """GET the session's context_url for a fresh brief at join time.

    Returns the parsed `context` object ({"meeting": ..., "brief_markdown": ...})
    or None on any failure — the caller keeps the booking-time brief.
    """
    url = _context_request_url(integration)
    if not url:
        return None
    headers = {}
    org_id = str((integration or {}).get("org_id") or "")
    token = _bearer_for(org_id, settings.laura_context_token)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=settings.callback_timeout_seconds) as client:
            resp = client.get(url, headers=headers)
            target = _redirect_target(resp)
            if target:
                resp = client.get(target, headers=headers)
        resp.raise_for_status()
        context = resp.json().get("context")
        got = isinstance(context, dict)
        # PII-safe telemetry: confirms the join-time context pull actually ran
        # ("non ha preso il contesto" was undiagnosable — success logged nothing).
        # A boolean on whether a brief came back, never the brief itself.
        print(
            f"[cedric-callback] context fetched (HTTP {resp.status_code}, "
            f"brief={'yes' if got and context.get('brief_markdown') else 'no'})",
            flush=True,
        )
        return context if got else None
    except Exception as e:  # noqa: BLE001 — never block the join on a refresh
        print(f"[cedric-callback] context refresh failed: {e}", flush=True)
        return None


def provision_org(
    org_id: str, team_id: str | None, channel: str = "", avatar_id: str = ""
) -> ProvisionResult | None:
    """Register a Laura org on the orchestrator (Connect the brain): POST the
    org→workspace link to CEDRIC_ORGS_URL so Cedric can route this org's events
    to its Slack team even without external_ref, and mint the org's own
    webhook credentials on his side.

    Returns a truthy ProvisionResult on 2xx (including both minted workspace
    credentials for immediate SSM write-through), a falsey result on refusal/error,
    or None when the endpoint isn't configured yet (the connection stays
    'pending'). Minted credentials are returned only in the redacted result;
    dashboard.py persists them as per-org SecureStrings and never logs them."""
    url = settings.cedric_orgs_url.strip()
    if not url:
        return None
    target_url = f"{url.rstrip('/')}/pending" if not team_id else url
    headers = {"Content-Type": "application/json"}
    token = settings.cedric_orgs_token.strip() or settings.laura_api_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "org_id": org_id,
        "team_id": team_id or None,
        "default_slack_channel": channel,
        "avatar_id": avatar_id,
    }
    try:
        with httpx.Client(timeout=settings.callback_timeout_seconds) as client:
            resp = client.post(target_url, json=payload, headers=headers)
            target = _redirect_target(resp)
            if target:
                resp = client.post(target, json=payload, headers=headers)
        secret = ""
        peer_token = ""
        if 200 <= resp.status_code < 300:
            try:
                data = resp.json()
                credentials = data.get("credentials") if isinstance(data, dict) else None
                if isinstance(credentials, dict):
                    candidate = credentials.get("webhook_secret")
                    secret = candidate.strip() if isinstance(candidate, str) else ""
                    candidate = credentials.get("webhook_token")
                    peer_token = candidate.strip() if isinstance(candidate, str) else ""
            except ValueError:
                pass
        print(f"[cedric-callback] org provisioning HTTP {resp.status_code}", flush=True)
        return ProvisionResult(resp.status_code, secret, peer_token)
    except Exception as e:  # noqa: BLE001 — connection stays pending, retry later
        print(
            f"[cedric-callback] org provisioning failed ({type(e).__name__})",
            flush=True,
        )
        return ProvisionResult(0)


def fetch_org_connectors(org_id: str, team_id: str = "") -> dict | None:
    """The product bridge, read side: what the brain can touch for this org.
    GET {orchestrator}/api/laura/connectors?org_id=&team= — returns Cedric's
    connector catalog with live state (connected / account label /
    needs-reconnect) plus browser connect_url/manage_url links. ``team_id``
    (the org's OWN Slack workspace, from its org_connections config) pins the
    upstream query to the caller's workspace so an org the orchestrator can't
    resolve never falls back to another team's catalog. PII-light by
    contract (no account ids or tokens); Laura renders it verbatim in the
    avatar's Configure tab and never stores it. None when the orchestrator
    isn't configured/linked or on any failure."""
    base = settings.cedric_orgs_url.strip()
    if not base:
        return None
    # CEDRIC_ORGS_URL points at .../api/laura/orgs — the sibling route.
    url = base.rstrip("/").rsplit("/", 1)[0] + "/connectors"
    headers = {}
    token = _bearer_for(
        org_id, settings.cedric_orgs_token.strip() or settings.laura_api_token.strip()
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {"org_id": org_id}
    if (team_id or "").strip():
        params["team"] = team_id.strip()
    try:
        with httpx.Client(timeout=settings.callback_timeout_seconds) as client:
            resp = client.get(url, params=params, headers=headers)
            target = _redirect_target(resp)
            if target:
                resp = client.get(target, params=params, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as e:  # noqa: BLE001 — the Configure tab just shows "unavailable"
        print(f"[cedric-callback] connectors fetch failed: {e}", flush=True)
        return None


def revoke_org(org_id: str) -> int | None:
    """Remote revoke, the write half of disconnect: DELETE the org→workspace
    link on the orchestrator (``DELETE {CEDRIC_ORGS_URL}/{org_id}`` — the
    contract's `/api/laura/orgs/{org_id}` mirror of provisioning). Returns the
    HTTP status code (0 on transport error / an org_id unsafe for a URL path),
    or None when CEDRIC_ORGS_URL isn't configured (no remote side exists —
    the caller may disconnect locally). The caller treats 2xx and 404
    (already gone) as revoked and MUST leave local state untouched on
    anything else — never claim a disconnection the orchestrator didn't
    confirm."""
    base = settings.cedric_orgs_url.strip()
    if not base:
        return None
    org = (org_id or "").strip()
    # Laura generates org ids, but validate before splicing one into a URL
    # path anyway (same rule as secret_registry._org_parameter_name).
    import re

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", org):
        return 0
    url = f"{base.rstrip('/')}/{org}"
    headers = {}
    token = _bearer_for(
        org, settings.cedric_orgs_token.strip() or settings.laura_api_token.strip()
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=settings.callback_timeout_seconds) as client:
            resp = client.delete(url, headers=headers)
            target = _redirect_target(resp)
            if target:
                resp = client.delete(target, headers=headers)
        print(f"[cedric-callback] org revoke HTTP {resp.status_code}", flush=True)
        return resp.status_code
    except Exception as e:  # noqa: BLE001 — local state must stay 'connected'
        print(f"[cedric-callback] org revoke failed ({type(e).__name__})", flush=True)
        return 0