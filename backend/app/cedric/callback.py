"""Callbacks to the orchestrator (Cedric) that booked a session.

A session started with a `callback_url` gets two kinds of events POSTed back
(see the Cedric X Laura project's docs/04-api-contract.md):

  - session.status  — best-effort, single attempt (joining / live / failed).
  - session.ended   — the full artifact; retried with backoff because losing it
                      means the orchestrator has to fall back to polling.

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

import httpx

from ..config import settings

# Retry schedule for session.ended (seconds between attempts). Module-level so
# tests can monkeypatch it to zeros.
ENDED_BACKOFF: tuple[float, ...] = (5.0, 25.0, 120.0)


def _signature_headers(body: bytes) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = settings.laura_webhook_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    secret = settings.laura_webhook_secret.strip()
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
    # A redirect (e.g. Vercel apex→www) is followed manually for one hop:
    # httpx's follow_redirects strips Authorization when the host changes, so
    # the auth + signature headers must be re-applied to the new URL.
    with httpx.Client(timeout=settings.callback_timeout_seconds) as client:
        resp = client.post(url, content=body, headers=_signature_headers(body))
        target = _redirect_target(resp)
        if target:
            resp = client.post(target, content=body, headers=_signature_headers(body))
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


def fetch_context(integration: dict | None) -> dict | None:
    """GET the session's context_url for a fresh brief at join time.

    Returns the parsed `context` object ({"meeting": ..., "brief_markdown": ...})
    or None on any failure — the caller keeps the booking-time brief.
    """
    url = (integration or {}).get("context_url") or ""
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
        return context if isinstance(context, dict) else None
    except Exception as e:  # noqa: BLE001 — never block the join on a refresh
        print(f"[cedric-callback] context refresh failed: {e}", flush=True)
        return None
