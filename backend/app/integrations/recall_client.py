"""Recall.ai client — gets the bot into the meeting and streams transcript back.

The bot does two things for us:
  1. Renders our avatar page as its camera (output_media kind=webpage).
  2. Streams finalized transcript utterances to our webhook (transcript.data),
     each tagged with the speaking participant's name (Recall's built-in
     diarization — no separate speaker-ID service needed for the MVP).

Field shapes follow Recall's current Create Bot schema. If your Recall API
version differs, the request body is isolated here — adjust in one place.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from urllib.parse import quote, urlparse

import httpx

from ..config import settings


class AvatarBusyError(RuntimeError):
    """Recall has no avatar bot capacity available for a new dispatch."""


# Transient-507 dispatch retry (2026-07-24: Recall's shared avatar pool ran
# dry twice with zero of our bots active — it clears in a minute or two, and
# a hard bounce mid-demo is worse than a short spinner).
_BUSY_RETRIES = 3
_BUSY_BACKOFF_S = (10, 15, 20)
_BUSY_SLEEP = time.sleep  # test seam: patched so suites never really wait


_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
_CLIENT = httpx.Client(
    timeout=_TIMEOUT,
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_OUTPUT_VARIANTS = ("web_gpu", "web_4_core")


def readiness() -> dict:
    """Return non-secret Recall setup status for diagnostics."""
    issues: list[str] = []
    key = settings.recall_api_key.strip()
    public_base_url = settings.public_base_url.strip()

    if not key:
        issues.append("RECALL_API_KEY is not set.")
        key_status = "missing"
    elif key.startswith("whsec_"):
        issues.append(
            "RECALL_API_KEY starts with whsec_, which is a webhook verification "
            "secret, not a Recall API key."
        )
        key_status = "webhook_secret"
    else:
        key_status = "set"

    parsed = urlparse(public_base_url)
    if not public_base_url:
        issues.append("PUBLIC_BASE_URL is not set.")
        public_url_status = "missing"
    elif "your-" in public_base_url or "placeholder" in public_base_url.lower():
        issues.append("PUBLIC_BASE_URL still looks like a placeholder.")
        public_url_status = "placeholder"
    elif parsed.scheme != "https":
        issues.append("PUBLIC_BASE_URL must be an HTTPS URL reachable by Recall.")
        public_url_status = "not_https"
    elif parsed.hostname in {"127.0.0.1", "localhost"}:
        issues.append("PUBLIC_BASE_URL cannot be localhost; use an ngrok/deploy URL.")
        public_url_status = "local"
    else:
        public_url_status = "set"

    return {
        "ready": not issues,
        "issues": issues,
        "api_base": settings.recall_api_base.rstrip("/"),
        "api_key": key_status,
        "public_base_url": public_url_status,
    }


def assert_ready() -> None:
    """Raise a user-actionable error before creating paid/live resources."""
    status = readiness()
    if status["ready"]:
        return
    raise RuntimeError("Recall is not ready: " + " ".join(status["issues"]))


def _api_key() -> str:
    key = settings.recall_api_key.strip()
    if not key:
        raise RuntimeError("RECALL_API_KEY is not set.")
    if key.startswith("whsec_"):
        raise RuntimeError(
            "RECALL_API_KEY starts with whsec_, which is a webhook verification "
            "secret, not a Recall API key."
        )
    return key


def _headers() -> dict:
    assert_ready()
    return {
        # Recall accepts the raw API key; using no prefix makes it harder to
        # confuse API keys with webhook secrets in debugging.
        "Authorization": _api_key(),
        "Content-Type": "application/json",
    }


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 5.0)
            except ValueError:
                pass
    return min(0.5 * (2 ** attempt), 4.0)


def _request(
    method: str,
    url: str,
    *,
    retry: bool = False,
    **kwargs,
) -> httpx.Response:
    attempts = 3 if retry else 1
    last_exc: Exception | None = None
    for attempt in range(attempts):
        resp: httpx.Response | None = None
        try:
            resp = _CLIENT.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
            last_exc = e
            if attempt == attempts - 1:
                raise
        else:
            if resp.status_code not in _RETRY_STATUSES or attempt == attempts - 1:
                return resp
            resp.close()

        time.sleep(_retry_delay(resp, attempt))

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Recall request failed without a response.")


def auth_check() -> dict:
    """Make a safe read-only API call to confirm Recall authentication."""
    status = readiness()
    if status["api_key"] != "set":
        return {**status, "auth": "skipped"}

    try:
        resp = _request(
            "GET",
            f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/",
            headers={
                "Authorization": _api_key(),
                "Content-Type": "application/json",
            },
        )
    except Exception as e:
        return {**status, "ready": False, "auth": "error", "error": str(e)}

    if 200 <= resp.status_code < 300:
        return {**status, "auth": "valid"}
    if resp.status_code == 401:
        return {
            **status,
            "ready": False,
            "auth": "invalid",
            "issues": status["issues"] + [
                "Recall rejected RECALL_API_KEY with 401. Check the key and region."
            ],
        }
    return {
        **status,
        "ready": False,
        "auth": "unexpected_status",
        "status_code": resp.status_code,
    }


def _transcript_provider_config(provider_override: str | None = None) -> dict:
    """Return the Recall recording_config.transcript.provider payload."""
    provider = (provider_override or settings.recall_transcription_provider).strip().lower()

    if provider in {"elevenlabs", "elevenlabs_streaming"}:
        config = {
            "model_id": (
                settings.elevenlabs_transcription_model.strip()
                or "scribe_v2_realtime"
            )
        }
        language_code = settings.elevenlabs_transcription_language_code.strip()
        if language_code:
            config["language_code"] = language_code
        return {"elevenlabs_streaming": config}

    if provider in {"deepgram", "deepgram_streaming"}:
        # Deepgram nova-3 with language=multi: automatic language detection +
        # code-switching (Italian/English mixed meetings). The Deepgram API key
        # + project id live in the RECALL DASHBOARD (per-region), not here.
        config = {"model": settings.deepgram_model.strip() or "nova-3"}
        language = settings.deepgram_language.strip()
        if language:
            config["language"] = language
        if settings.deepgram_endpointing_ms > 0:
            config["endpointing"] = settings.deepgram_endpointing_ms
        return {"deepgram_streaming": config}

    if provider in {"recallai", "recallai_streaming"}:
        return {
            "recallai_streaming": {
                "mode": (
                    settings.recall_transcription_mode.strip()
                    or "prioritize_low_latency"
                ),
                "language_code": (
                    settings.recall_transcription_language_code.strip() or "en"
                ),
            }
        }

    raise RuntimeError(
        "Unknown RECALL_TRANSCRIPTION_PROVIDER "
        f"'{settings.recall_transcription_provider}'. "
        "Use 'recallai', 'elevenlabs' or 'deepgram'."
    )


def _variant_payload(variant: str | None) -> dict[str, str] | None:
    if not variant:
        return None
    return {
        "zoom": variant,
        "google_meet": variant,
        "microsoft_teams": variant,
    }


def _ears_ws_url(realtime_capability: str) -> str:
    """wss:// URL for the mixed-audio realtime endpoint (Gemini ears).

    Points at the Cloudflare Worker RELAY (ears_relay_ws_base), NOT the backend:
    AWS App Runner rejects inbound WebSockets, so Recall's audio can't reach it.
    The relay accepts the audio WS, runs the Gemini session, and POSTs turns
    back to the backend over HTTP. Capability goes in the PATH (survives URL
    normalization; the query-param form was never reached in the first live run).
    """
    base = settings.ears_relay_ws_base.strip().rstrip("/")
    return f"{base}/realtime/recall-audio/{realtime_capability}"


def _create_bot_body(
    meeting_url: str,
    avatar_page_url: str,
    *,
    join_at: str | None,
    provider: dict,
    variant: str | None,
    bot_name: str = "Laura",
    realtime_capability: str,
) -> dict:
    # Recall realtime endpoints are not Svix-signed. Bind the URL to this one
    # bot with an unpredictable capability; only its SHA-256 is persisted.
    webhook_url = (
        f"{settings.public_base_url.rstrip('/')}/webhooks/recall"
        f"?cap={realtime_capability}"
    )

    body = {
        "meeting_url": meeting_url,
        "bot_name": bot_name or "Laura",
        "recording_config": {
            "transcript": {
                "provider": provider,
            },
            # Must be enabled for the participant_events.* realtime events below
            # to be delivered at all.
            "participant_events": {},
            # Real-time transcript utterances delivered here. Partials arrive
            # WHILE someone is still talking — they power barge-in and the
            # instant ack; finals (transcript.data) drive the actual answers.
            # participant_events give the avatar a live roster (who is in the
            # room, including people who never speak) — without them she can't
            # know "we are 3 in this meeting" or address people by name.
            "realtime_endpoints": [
                {
                    "type": "webhook",
                    "url": webhook_url,
                    "events": [
                        "transcript.data",
                        "transcript.partial_data",
                        "participant_events.join",
                        "participant_events.leave",
                    ],
                }
            ],
        },
        # Top-level: the bot's camera renders our avatar page (Anam face inside).
        # Shape per Recall's Output Media API: camera → kind=webpage → config.url.
        "output_media": {
            "camera": {
                "kind": "webpage",
                "config": {"url": avatar_page_url},
            }
        },
    }
    variant_payload = _variant_payload(variant)
    if variant_payload:
        body["variant"] = variant_payload
    if join_at:  # schedule the bot to join at this time instead of now
        body["join_at"] = join_at
    return body


def _create_bot_attempts(
    meeting_url: str,
    avatar_page_url: str,
    join_at: str | None,
    bot_name: str = "Laura",
    realtime_capability: str = "",
    attach_ears: bool = False,
    attach_voice_agent_url: str = "",
) -> list[tuple[str, dict]]:
    configured_provider = _transcript_provider_config()
    attempts: list[tuple[str, dict]] = []

    for variant in _OUTPUT_VARIANTS:
        attempts.append(
            (
                f"{variant}/configured-transcription",
                _create_bot_body(
                    meeting_url,
                    avatar_page_url,
                    join_at=join_at,
                    provider=configured_provider,
                    variant=variant,
                    bot_name=bot_name,
                    realtime_capability=realtime_capability,
                ),
            )
        )

    # If Recall rejects a premium transcription provider that is not enabled in
    # the Recall workspace (no vendor key in the Recall dashboard, plan tier),
    # still get Laura into the meeting with Recall's built-in low-latency
    # English transcription instead of failing the invite.
    if "recallai_streaming" not in configured_provider:
        recallai_provider = _transcript_provider_config("recallai")
        attempts.append(
            (
                "web_4_core/recallai-transcription",
                _create_bot_body(
                    meeting_url,
                    avatar_page_url,
                    join_at=join_at,
                    provider=recallai_provider,
                    variant="web_4_core",
                    bot_name=bot_name,
                    realtime_capability=realtime_capability,
                ),
            )
        )
        attempts.append(
            (
                "default-web/recallai-transcription",
                _create_bot_body(
                    meeting_url,
                    avatar_page_url,
                    join_at=join_at,
                    provider=recallai_provider,
                    variant=None,
                    bot_name=bot_name,
                    realtime_capability=realtime_capability,
                ),
            )
        )
    else:
        attempts.append(
            (
                "default-web/configured-transcription",
                _create_bot_body(
                    meeting_url,
                    avatar_page_url,
                    join_at=join_at,
                    provider=configured_provider,
                    variant=None,
                    bot_name=bot_name,
                    realtime_capability=realtime_capability,
                ),
            )
        )

    if attach_ears:
        # Gemini ears: Recall streams the meeting's mixed raw audio (s16le
        # 16 kHz mono) over a websocket realtime endpoint — audio volume is
        # too high for webhooks. Ears-enabled copies of every attempt go
        # FIRST; the untouched originals remain as fallback, so a Recall 4xx
        # on the audio config can never keep Laura out of a meeting.
        import copy

        audio_endpoint = {
            "type": "websocket",
            "url": _ears_ws_url(realtime_capability),
            "events": ["audio_mixed_raw.data"],
        }
        eared: list[tuple[str, dict]] = []
        for label, plain_body in attempts:
            body = copy.deepcopy(plain_body)
            body["recording_config"]["audio_mixed_raw"] = {}
            body["recording_config"]["realtime_endpoints"] = list(
                body["recording_config"]["realtime_endpoints"]
            ) + [audio_endpoint]
            eared.append((f"{label}+gemini-ears", body))
        attempts = eared + attempts

    if attach_voice_agent_url:
        # ElevenLabs Agent runtime: stream the meeting audio to the
        # cedric-voice bridge. SEPARATE per-participant streams first (each
        # participant's mic on its own WS — the bot's output is NOT a stream,
        # so no self-hearing and no half-duplex deaf window, and true voice
        # barge-in works). Recall gates the feature behind a workspace flag,
        # so MIXED-audio copies ride as the next rung ("+voice-agent" = the
        # proven half-duplex path), then the untouched originals — a Recall
        # 4xx can never keep the avatar out of the meeting. The created-with
        # label in the backend log says which rung won.
        import copy

        sep_endpoint = {
            "type": "websocket",
            "url": attach_voice_agent_url,
            "events": ["audio_separate_raw.data"],
        }
        mixed_endpoint = {
            "type": "websocket",
            "url": attach_voice_agent_url,
            "events": ["audio_mixed_raw.data"],
        }
        voiced_sep: list[tuple[str, dict]] = []
        voiced_mixed: list[tuple[str, dict]] = []
        for label, plain_body in attempts:
            body = copy.deepcopy(plain_body)
            body["recording_config"]["audio_separate_raw"] = {}
            body["recording_config"]["realtime_endpoints"] = list(
                body["recording_config"]["realtime_endpoints"]
            ) + [sep_endpoint]
            voiced_sep.append((f"{label}+voice-sep", body))
            body = copy.deepcopy(plain_body)
            body["recording_config"]["audio_mixed_raw"] = {}
            body["recording_config"]["realtime_endpoints"] = list(
                body["recording_config"]["realtime_endpoints"]
            ) + [mixed_endpoint]
            voiced_mixed.append((f"{label}+voice-agent", body))
        attempts = voiced_sep + voiced_mixed + attempts

    return attempts


def verify_webhook(raw_body: bytes, headers: Mapping[str, str]) -> None:
    """Verify a Recall webhook if RECALL_WEBHOOK_SECRET is configured.

    Recall's whsec_ value is for HMAC verification of incoming webhook requests.
    It is intentionally separate from RECALL_API_KEY, which authenticates API
    calls that create or control bots.
    """
    secret = settings.recall_webhook_secret.strip()
    if not secret:
        return
    if not secret.startswith("whsec_"):
        raise RuntimeError("RECALL_WEBHOOK_SECRET must start with whsec_.")

    msg_id = headers.get("webhook-id") or headers.get("svix-id")
    timestamp = headers.get("webhook-timestamp") or headers.get("svix-timestamp")
    signature_header = (
        headers.get("webhook-signature") or headers.get("svix-signature")
    )
    if not msg_id or not timestamp or not signature_header:
        raise RuntimeError("Recall webhook signature headers are missing.")

    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed_payload = b".".join([msg_id.encode(), timestamp.encode(), raw_body])
    expected = hmac.new(key, signed_payload, hashlib.sha256).digest()

    for versioned_sig in signature_header.split():
        try:
            version, encoded_sig = versioned_sig.split(",", 1)
        except ValueError:
            continue
        if version != "v1":
            continue
        try:
            actual = base64.b64decode(encoded_sig)
        except Exception:
            continue
        if len(actual) == len(expected) and hmac.compare_digest(actual, expected):
            return

    raise RuntimeError("Recall webhook signature verification failed.")


def delete_bot(bot_id: str) -> None:
    """Delete a SCHEDULED bot (one that has not joined yet). Live bots reject
    this — use leave_call for them; the cancel endpoint tries both."""
    resp = _request(
        "DELETE",
        f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/{bot_id}/",
        headers=_headers(),
        retry=True,
    )
    resp.raise_for_status()


def create_bot(
    meeting_url: str,
    avatar_page_url: str,
    join_at: str | None = None,
    bot_name: str = "Laura",
    avatar_id: str = "",
) -> dict:
    """Send a bot into `meeting_url` showing `avatar_page_url` on its camera.

    If `join_at` (ISO 8601, >=10 min in the future) is given, Recall SCHEDULES the
    bot to join then — this is how calendar auto-join dispatches bots ahead of time.
    Returns the created bot object (includes its `id`).

    Whether the bot streams audio to the Gemini ears relay is decided PER AVATAR
    (dashboard brain choice), resolved here from `avatar_id`.
    """
    from . import elevenlabs_agent, gemini_ears

    realtime_capability = secrets.token_urlsafe(32)
    # ElevenLabs Agent runtime (Cedric pilot): when this avatar resolves to the
    # agent runtime AND the cedric-voice bridge is configured, the bot streams
    # its audio THERE — and Gemini ears must not attach (exactly one system may
    # own the meeting audio). The page URL gains the voice-out coordinates so
    # the avatar's browser can play the agent's streamed voice.
    voice_agent_url = ""
    voice_base = settings.voice_agent_relay_ws_base.strip().rstrip("/")
    el_runtime, _el_agent_id = elevenlabs_agent.runtime_for_avatar_id(avatar_id)
    if el_runtime == elevenlabs_agent.RUNTIME_ELEVENLABS_AGENT and voice_base:
        voice_agent_url = f"{voice_base}/voice/{realtime_capability}"
        sep = "&" if "?" in avatar_page_url else "?"
        avatar_page_url = (
            f"{avatar_page_url}{sep}voice_ws={quote(voice_base, safe='')}"
            f"&voice_cap={realtime_capability}"
        )
    attach_ears = (
        not voice_agent_url
        and gemini_ears.mode_enabled(gemini_ears.mode_for_avatar(avatar_id))
        and bool(settings.ears_relay_ws_base.strip())
    )
    attempts = _create_bot_attempts(
        meeting_url,
        avatar_page_url,
        join_at,
        bot_name,
        realtime_capability,
        attach_ears=attach_ears,
        attach_voice_agent_url=voice_agent_url,
    )
    last_error: httpx.HTTPStatusError | None = None
    _busy_round = 0
    while True:
        _busy_retry = False

        for idx, (label, body) in enumerate(attempts):
            resp = _request(
                "POST",
                f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/",
                headers=_headers(),
                json=body,
                retry=True,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    raise RuntimeError(
                        "Recall rejected RECALL_API_KEY with 401. Make sure it is the "
                        "API Key, not a whsec_ workspace/webhook secret, and that "
                        "RECALL_API_BASE matches the key's region."
                    ) from e
                if e.response.status_code == 507:
                    # Recall's shared avatar pool is momentarily out of capacity
                    # (hit twice on 2026-07-24 with ZERO of our bots active — it's
                    # their side and usually clears in a minute or two). Retry the
                    # SAME dispatch a few times with a short backoff before giving
                    # up, so a transient blip never bounces a live demo. Sync sleep
                    # is fine: create_bot runs in the threadpool, off the loop.
                    if _busy_round < _BUSY_RETRIES:
                        wait = _BUSY_BACKOFF_S[min(_busy_round,
                                                   len(_BUSY_BACKOFF_S) - 1)]
                        print(
                            f"[recall] avatar capacity busy (507) — retry "
                            f"{_busy_round + 1}/{_BUSY_RETRIES} in {wait}s",
                            flush=True,
                        )
                        _BUSY_SLEEP(wait)
                        _busy_round += 1
                        _busy_retry = True
                        break  # restart the attempts loop from the first config
                    # Do not bubble Recall's raw response body to the product UI.
                    raise AvatarBusyError(
                        "All avatars are busy right now — retry in a minute."
                    ) from e
                if e.response.status_code == 400 and idx < len(attempts) - 1:
                    last_error = e
                    print(
                        f"[recall] create_bot rejected {label}; trying fallback",
                        flush=True,
                    )
                    continue
                raise
            result = resp.json()
            # Which attempt won matters operationally (did the bot get the ears
            # audio endpoint, or a fallback?) — label only, no meeting content.
            print(f"[recall] bot created via {label}", flush=True)
            # Private hand-off to main.py. This key is removed before any API
            # response is built and the raw capability is never persisted/logged.
            result["_laura_realtime_capability"] = realtime_capability
            return result

        if _busy_retry:
            continue  # transient 507 — run the attempts again
        if last_error:
            raise last_error
        raise RuntimeError("Recall create_bot failed without a response.")


def create_calendar(
    *,
    oauth_client_id: str,
    oauth_client_secret: str,
    oauth_refresh_token: str,
    oauth_email: str = "",
    metadata: dict | None = None,
) -> dict:
    """Create a Recall Calendar V2 connection from Google OAuth credentials."""
    body = {
        "platform": "google_calendar",
        "oauth_client_id": oauth_client_id,
        "oauth_client_secret": oauth_client_secret,
        "oauth_refresh_token": oauth_refresh_token,
    }
    if oauth_email:
        body["oauth_email"] = oauth_email
    if metadata:
        body["metadata"] = metadata

    resp = _request(
        "POST",
        f"{settings.recall_api_base.rstrip('/')}/api/v2/calendars/",
        headers=_headers(),
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


def list_calendars() -> list[dict]:
    """List Recall Calendar V2 connections (read-only; dashboard upcoming view)."""
    resp = _request(
        "GET",
        f"{settings.recall_api_base.rstrip('/')}/api/v2/calendars/",
        headers=_headers(),
        retry=True,
    )
    resp.raise_for_status()
    data = resp.json()
    rows = data if isinstance(data, list) else (data.get("results") or [])
    return [c for c in rows if isinstance(c, dict)]


def list_calendar_events(
    *, calendar_id: str, updated_at_gte: str = "", is_deleted: bool = False
) -> list[dict]:
    """Fetch Recall Calendar V2 events for a sync webhook."""
    params: dict[str, object] = {
        "calendar_id": calendar_id,
        "is_deleted": str(is_deleted).lower(),
    }
    if updated_at_gte:
        params["updated_at__gte"] = updated_at_gte

    events: list[dict] = []
    url = f"{settings.recall_api_base.rstrip('/')}/api/v2/calendar-events/"
    while url:
        resp = _request("GET", url, headers=_headers(), params=params, retry=True)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            events.extend(e for e in data if isinstance(e, dict))
            break

        rows = (
            data.get("results")
            or data.get("calendar_events")
            or data.get("data")
            or []
        )
        events.extend(e for e in rows if isinstance(e, dict))
        url = data.get("next") or ""
        params = {}
    return events


def leave_call(bot_id: str) -> None:
    """Remove the bot from the meeting (stops avatar streaming → stops billing).

    Hardened like delete_bot: retry the transient 5xx/429 window, then
    raise_for_status so a PERSISTENT Recall failure propagates to the caller.
    Swallowing a 5xx (the old behavior) let finalize believe the meter had
    stopped, delete the session, and leak the per-minute bill forever — the
    reconcile backstop only revisits sessions still in the store, so a
    removed-but-still-live bot was never retried.
    """
    resp = _request(
        "POST",
        f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/{bot_id}/leave_call/",
        headers=_headers(),
        retry=True,
    )
    resp.raise_for_status()


def send_chat_message(bot_id: str, message: str) -> None:
    """Post a line into the meeting chat (visible to everyone).

    Recall has no raise-hand action on any platform, so the chat message is
    the in-platform half of the avatar's hand-raise (the visual half is the
    gesture on her /talk tile). Best-effort at the call site — a chat failure
    must never block or delay the live path.
    """
    _request(
        "POST",
        f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/{bot_id}/send_chat_message/",
        headers=_headers(),
        json={"to": "everyone", "message": message[:500]},
    )
