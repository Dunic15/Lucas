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
carry `Authorization: Bearer LAURA_WEBHOOK_TOKEN` as a cheap first-line check.

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
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..config import settings

# Retry schedule for session.ended (seconds between attempts). Module-level so
# tests can monkeypatch it to zeros.
ENDED_BACKOFF: tuple[float, ...] = (5.0, 25.0, 120.0)


def _secret_for(org_id: str) -> str:
    """The signing secret for an org: its entry in the per-client registry
    (LAURA_WEBHOOK_SECRETS_BY_ORG, a JSON object {org_id: secret}) when
    present, else the global LAURA_WEBHOOK_SECRET. The registry is how each
    connected workspace gets its own credential (minted by the orchestrator's
    /api/laura/orgs provisioning) without rotating anyone else's."""
    raw = settings.laura_webhook_secrets_by_org.strip()
    if raw and org_id:
        try:
            per_org = json.loads(raw).get(org_id, "")
            if isinstance(per_org, str) and per_org.strip():
                return per_org.strip()
        except (ValueError, AttributeError):
            print("[cedric-callback] LAURA_WEBHOOK_SECRETS_BY_ORG is not valid JSON", flush=True)
    return settings.laura_webhook_secret.strip()


def _signature_headers(body: bytes, org_id: str = "") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = settings.laura_webhook_token.strip()
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
    token = settings.laura_context_token.strip()
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
    org_id: str, team_id: str, channel: str = "", avatar_id: str = ""
) -> bool | None:
    """Register a Laura org on the orchestrator (Connect the brain): POST the
    org→workspace link to CEDRIC_ORGS_URL so Cedric can route this org's events
    to its Slack team even without external_ref, and mint the org's own
    webhook credentials on his side.

    Returns True on 2xx, False on refusal/error, None when the endpoint isn't
    configured yet (the connection stays 'pending' — contract step B, Cedric's
    /api/laura/orgs, is in flight). Best-effort: any minted credentials in the
    response are handled by ops (the signing registry env), NEVER stored or
    logged here."""
    url = settings.cedric_orgs_url.strip()
    if not url:
        return None
    headers = {"Content-Type": "application/json"}
    token = settings.cedric_orgs_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "org_id": org_id,
        "team_id": team_id,
        "default_slack_channel": channel,
        "avatar_id": avatar_id,
    }
    try:
        with httpx.Client(timeout=settings.callback_timeout_seconds) as client:
            resp = client.post(url, json=payload, headers=headers)
            target = _redirect_target(resp)
            if target:
                resp = client.post(target, json=payload, headers=headers)
        ok = 200 <= resp.status_code < 300
        print(f"[cedric-callback] org provisioning HTTP {resp.status_code}", flush=True)
        return ok
    except Exception as e:  # noqa: BLE001 — connection stays pending, retry later
        print(f"[cedric-callback] org provisioning failed: {e}", flush=True)
        return False
