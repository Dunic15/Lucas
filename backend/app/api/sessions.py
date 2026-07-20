from __future__ import annotations
"""Session lifecycle routes: /sessions/start + /sessions/{bot_id}/{context,end,
cancel,deliver,artifact,redeliver}. Extracted from main.py (Path A: the shared
meeting-lifecycle helpers stay resolvable on main via a function-local
`import app.main as _main`, preserving monkeypatch identity)."""
import hmac, asyncio, weakref
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.config import settings
from app import (auth, store, recall_client, anam_client, cedric, entitlements,
                 avatars, actions, gpu_runtime, runpod_runtime, control_plane, ledger)

router = APIRouter()


class StartRequest(BaseModel):
    meeting_url: str
    avatar_id: str = ""  # empty -> settings.default_avatar_id
    join_at: Optional[str] = None  # ISO 8601; set (>=10 min out) to schedule the bot
    # CEDRIC: orchestrator integration fields — all optional; models, the auth
    # gate, and validation live in the `cedric` package (docs/04-api-contract.md).
    context: Optional[cedric.MeetingContext] = None
    callback_url: Optional[str] = None   # where session.status/.ended events go
    context_url: Optional[str] = None    # re-fetched at join time for a fresh brief
    external_ref: Optional[dict] = None  # opaque, echoed verbatim in callbacks


class DeliverRequest(BaseModel):
    to: list[str] = []
    slack: bool = True


_start_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _start_lock_for(meeting_url: str) -> asyncio.Lock:
    key = ledger.meeting_key(meeting_url)
    lock = _start_locks.get(key)
    if lock is None:  # create-on-demand; get→create is synchronous (atomic here)
        lock = asyncio.Lock()
        _start_locks[key] = lock
    return lock


def _existing_session_clash(meeting_url: str, caller_org: str) -> JSONResponse | None:
    """The 409 to return when a local-store session is already booked for this
    exact meeting_url — one live/scheduled booking per URL (rebooking must cancel
    first, else two bots + two per-minute meters land in one call). None when
    there is no clash. Another tenant's clashing bot_id is never leaked."""
    for existing in store.all_sessions():
        if existing.meeting_url == meeting_url:
            if existing.org_id != caller_org:
                return JSONResponse(
                    {"error": "a session already exists for this meeting_url"},
                    status_code=409,
                )
            return JSONResponse(
                {
                    "error": "a session already exists for this meeting_url",
                    "bot_id": existing.bot_id,
                },
                status_code=409,
            )
    return None


def _schedule_start_reconcile(meeting_url: str, bot_id: str) -> None:
    """Schedule the post-start duplicate-bot reconcile without blocking the
    response (the Gmail loop can await it inline; a request handler cannot)."""
    try:
        asyncio.create_task(_reconcile_after_start(meeting_url, bot_id))
    except RuntimeError:
        pass  # no running loop to schedule on (shouldn't happen in the handler)


async def _reconcile_after_start(meeting_url: str, bot_id: str) -> None:
    """Give a racing duplicate bot a moment to register with Recall, then keep
    the best variant and drop the rest — same as the Gmail auto-join loop, but
    fire-and-forget so the /sessions/start response returns immediately."""
    import app.main as _main  # Path A: shared meeting-lifecycle helpers resolve on main at call time
    try:
        await asyncio.sleep(4)
        await run_in_threadpool(_main._reconcile_duplicate_bots, meeting_url, bot_id)
    except Exception:
        pass


def _org_token_bearer_org(request: Request) -> Optional[str]:
    """The org owning the request's Bearer, when it is a PER-ORG machine token
    (org_tokens: durable control plane first, SQLite fallback). None for no/
    non-org bearers — including the GLOBAL laura_api_token, which keeps its
    demo-org behavior. Sibling of cedric.resolve_machine_org (PR D), which
    additionally maps the global bearer to the Demo org — kept separate so PR
    A's start/end/redeliver semantics stay untouched.
    A raw secret is compared/hashed, never logged. Called
    on /sessions/start, /sessions/{id}/end and /sessions/{id}/redeliver —
    control-plane paths, never the live hot path. SYNC (SQLite + optionally
    the Postgres control plane): async handlers must call it via
    run_in_threadpool so it never blocks the shared event loop (single
    instance — a blocked loop stalls every live meeting)."""
    provided = request.headers.get("authorization", "")
    if not provided.startswith("Bearer "):
        return None
    raw = provided[len("Bearer "):].strip()
    if not raw:
        return None
    global_token = settings.laura_api_token.strip()
    if global_token and hmac.compare_digest(raw, global_token):
        return settings.demo_org_id
    # Durable revocation is authoritative in production: never resurrect a
    # token from the ephemeral SQLite cache after Postgres rejects it.
    if control_plane.enabled():
        return control_plane.resolve_org_token(raw)
    return store.resolve_org_token(raw)


@router.post("/sessions/start")
async def start_session(req: StartRequest, request: Request) -> JSONResponse:
    # A logged-in human (dashboard cookie) or a machine bearer (Cedric). The
    # shared auth.gate closes the "login enabled + no token" hole: an anonymous
    # caller can NOT dispatch a per-minute bot on a login-protected deployment.
    # A valid cookie stamps the session with the user's org for later scoping.
    # A PER-ORG machine bearer (org_tokens) both authenticates the start and
    # scopes it to ITS org — the service twin of the cookie principal.
    import app.main as _main  # Path A: shared meeting-lifecycle helpers resolve on main at call time
    user = auth.current_user(request)
    token_org: Optional[str] = None
    if user is None:
        # Threadpooled: the resolver is sync DB I/O (see its docstring).
        token_org = await run_in_threadpool(_org_token_bearer_org, request)
        if token_org is None:
            if err := auth.gate(request):
                return err
    # Browser/cookie users choose only meeting + avatar. Integration wiring
    # is server-owned per org; reject it before any vendor readiness check,
    # database enumeration or paid bot creation.
    if user is not None and (
        req.callback_url or req.context_url or req.external_ref
    ):
        return JSONResponse(
            {"error": "integration wiring is managed by your workspace"},
            status_code=400,
        )
    try:
        recall_client.assert_ready()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    brief = req.context.brief_markdown if req.context else ""  # CEDRIC
    if err := cedric.brief_too_large(brief):  # CEDRIC
        return err
    # The owning tenant: the logged-in user's org, else the org-token's org,
    # else the Demo org for the global-bearer/anon service path (Cedric
    # bearer, key-free demo). NEVER derived from a request body field or a
    # meeting participant (MULTI-TENANCY §0/§3.1) — StartRequest deliberately
    # has no org field; keep it that way.
    caller_org = user["org_id"] if user else (token_org or settings.demo_org_id)
    # Internal personas (INTERNAL_AVATAR_IDS) are not dispatchable by ANY
    # caller — same 404 an unknown avatar id gets (defense-in-depth while the
    # folder still exists; see config.internal_avatar_ids).
    if avatars.is_internal(req.avatar_id or settings.default_avatar_id):
        return JSONResponse({"error": "unknown avatar_id"}, status_code=404)
    # One live/scheduled booking per meeting URL: rebooking must cancel first
    # (otherwise two bots — and two per-minute meters — end up in one call).
    # Fast path: an obvious local-store clash needs no lock or Recall round-trip
    # (the common re-click of the same link).
    if clash := _existing_session_clash(req.meeting_url, caller_org):
        return clash

    if not cedric.request_integration_urls_allowed(req, caller_org):
        return JSONResponse(
            {"error": "callback_url/context_url must use the configured Cedric HTTPS origin"},
            status_code=400,
        )
    integration = cedric.build_integration(req, brief)  # CEDRIC
    if integration is not None:
        integration = {**integration, "org_id": caller_org}
    # Serialize the guard→create window PER MEETING so two concurrent starts for
    # the same link can't both pass the dedup checks and both create a bot.
    # DIFFERENT meetings hold different locks and still dispatch in parallel.
    async with _start_lock_for(req.meeting_url):
        # Re-check under the lock: a racing double-click may have created the
        # session between the fast-path check above and acquiring the lock.
        if clash := _existing_session_clash(req.meeting_url, caller_org):
            return clash
        # Durable cross-instance guard (Recall = source of truth): a redeploy can
        # wipe the local store while Recall still holds the live bot, so a
        # re-click would dispatch a SECOND bot + meter into the same call. The
        # Gmail/calendar paths already gate on this; wire it here too (now
        # platform-aware for Zoom/Teams as well as Meet).
        if await run_in_threadpool(_main._meeting_has_active_bot, req.meeting_url):
            return JSONResponse(
                {"error": "a session already exists for this meeting_url"},
                status_code=409,
            )
        try:
            result = await _main._start_avatar_session(
                req.meeting_url, req.avatar_id, req.join_at, integration,
                org_id=caller_org,
            )
        except recall_client.AvatarBusyError:
            return JSONResponse(
                {
                    "error": "avatar_busy",
                    "detail": "All avatars are busy right now — retry in a minute.",
                },
                status_code=503,
                headers={"Retry-After": "60"},
            )
        except entitlements.EntitlementsUnavailable:
            # Billing DB outage: fail CLOSED before any vendor dispatch —
            # never hand out unmetered paid minutes because Postgres blinked.
            return JSONResponse({"error": "billing_unavailable"}, status_code=503)
        except entitlements.UsageDenied as e:
            if e.reason == "active_session_exists":
                # One concurrent meeting per org (DB-enforced partial unique).
                return JSONResponse(
                    {"error": "active_session_exists"}, status_code=409
                )
            # Free allowance exhausted — the upgrade path (PR C) is the fix.
            return JSONResponse(
                {
                    "error": "usage_limit_reached",
                    "remaining_seconds": 0,
                    "checkout_path": "/billing/checkout",
                },
                status_code=402,
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    # Resolve any deploy-overlap duplicate the way the Gmail loop does, without
    # holding up the response.
    _schedule_start_reconcile(req.meeting_url, result["bot_id"])
    return JSONResponse(result)


@router.post("/sessions/{bot_id}/context")
async def push_context(
    bot_id: str, req: cedric.ContextPush, request: Request
) -> JSONResponse:
    caller_org = await run_in_threadpool(cedric.resolve_machine_org, request)
    if caller_org is None:
        if err := cedric.auth_error(request):  # CEDRIC
            return err
    return cedric.apply_context_push(store.get(bot_id), req, caller_org)


@router.post("/sessions/{bot_id}/end")
async def end_session(bot_id: str, request: Request) -> JSONResponse:
    # Cookie (dashboard) or bearer (machine). auth.gate blocks an anonymous
    # caller from force-ending a bot (DoS + meter) on a login-protected
    # deployment. A logged-in user may end their own org's and unowned/service
    # sessions — never another org's. A PER-ORG machine bearer (org_tokens)
    # may end ONLY sessions of ITS org — never demo/unowned/another org's —
    # so a token that can start a session can also stop its meter (PR D
    # symmetry) without gaining the global bearer's reach.
    import app.main as _main  # Path A: shared meeting-lifecycle helpers resolve on main at call time
    user = auth.current_user(request)
    token_org: Optional[str] = None
    if user is None:
        # Threadpooled: sync DB I/O (see _org_token_bearer_org's docstring).
        token_org = await run_in_threadpool(_org_token_bearer_org, request)
        if token_org is None:
            if err := auth.gate(request):
                return err
    else:
        live = store.get(bot_id)
        # A logged-in user may end their own org's and legacy unowned ("")
        # sessions. Demo-org sessions are NOT theirs — self-serve product
        # decision (2026-07-13): the Demo org is the anonymous showroom, and a
        # real signup must not be able to kill (or see) another visitor's demo.
        if live is not None and live.org_id not in ("", user["org_id"]):
            return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    if token_org is not None:
        live = store.get(bot_id)
        if live is not None and live.org_id != token_org:
            return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    artifact_scope = (
        token_org
        or (str(user["org_id"]) if user is not None else None)
    )
    artifact = await _main._finalize_session(
        bot_id, source="manual", artifact_org_id=artifact_scope
    )
    if artifact is None:
        # _main._finalize_session returns None only when the session is already gone
        # AND no artifact was stored — i.e. a genuinely unknown bot, OR a
        # concurrent terminal-webhook/reconcile finalize still in flight (its
        # artifact isn't saved until late in the body). Distinguish the two: a
        # bare 404 for a bot Cedric just had live is a misleading signal, so
        # answer 202 "finalizing" while another path owns it.
        if bot_id in _main._finalizing or store.get(bot_id) is not None:
            return JSONResponse({"ok": True, "finalizing": bot_id}, status_code=202)
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    artifact_org = str(artifact.get("org_id") or "")
    if user is not None and artifact_org not in ("", str(user["org_id"])):
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    if token_org is not None and artifact_org != token_org:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    return JSONResponse(cedric.wire_artifact(artifact))  # CEDRIC: PII stays home


@router.post("/sessions/{bot_id}/cancel")
async def cancel_session(bot_id: str, request: Request) -> JSONResponse:
    """Cancel a scheduled bot / abort a live one WITHOUT building an artifact.

    Used by the orchestrator when a calendar event moves or is cancelled (it
    rebooks afterwards). `end` keeps its meaning: finalize + artifact.
    """
    import app.main as _main  # Path A: shared meeting-lifecycle helpers resolve on main at call time
    # PER-ORG machine bearers are first-class here (PR D): they authenticate
    # like the global bearer but may cancel ONLY their own org's sessions. The
    # global bearer keeps its full legacy service scope; key-free stays open.
    machine_org = await run_in_threadpool(cedric.resolve_machine_org, request)
    if machine_org is None:
        if err := cedric.auth_error(request):  # CEDRIC
            return err
    session = store.get(bot_id)
    # Wrong-org answers the IDENTICAL body as not-found: a distinct 403 would
    # be an existence oracle (a per-org bearer probing whether another org's
    # bot_id exists). Adversarial review 2026-07-13, should-fix 2.
    if session is None or (
        machine_org is not None
        and session.org_id != machine_org
    ):
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    # Stop the meter: works for live bots; scheduled bots may reject leave_call,
    # so fall back to deleting the scheduled bot. Track whether the meter is
    # CONFIRMED off (leave succeeded, bot already gone 404/410, or the scheduled
    # bot was deleted) — PR B BLOCKER 2 gates the slot-release on that.
    meter_off = False
    try:
        await run_in_threadpool(recall_client.leave_call, bot_id)
        meter_off = True
    except Exception as e:  # noqa: BLE001 — classified by _main._leave_confirmed_stopped
        if _main._leave_confirmed_stopped(e):
            meter_off = True  # 404/410: bot genuinely gone → not billing
        else:
            try:
                await run_in_threadpool(recall_client.delete_bot, bot_id)
                meter_off = True  # a scheduled bot deleted → never billed
            except Exception as e2:  # noqa: BLE001 — surface but don't fail cancel
                print(f"[sessions] cancel: recall cleanup failed: {e2}", flush=True)
    if session.anam_conversation_id:
        try:
            await run_in_threadpool(
                anam_client.end_conversation, session.anam_conversation_id
            )
        except Exception:
            pass
    if not meter_off:
        # Meter-stop UNVERIFIED (Recall 5xx/network on BOTH leave and delete):
        # the bot may still be live+billing. Keep the session (leave_pending) so
        # the reconcile backstop retries the leave, and DO NOT close the usage
        # row / free the slot — a 2nd meeting for this org stays correctly
        # refused until the meter is confirmed off. _retry_leave closes the row.
        session.leave_pending = True
        session.usage_close_reason = "cancelled"
        session.usage_end_epoch = None
        print(
            f"[sessions] cancel: leave unverified — bot={bot_id} kept for "
            f"reconcile meter-stop retry",
            flush=True,
        )
        return JSONResponse(
            {"cancelled": False, "leave_pending": bot_id}, status_code=202
        )
    # PR B: meter confirmed off → release the usage row (a never-joined
    # scheduled bot closes 0; a live one closes with its elapsed) so the org
    # can book its next meeting.
    await _main._close_usage_for(session.org_id, bot_id, None, "cancelled")
    store.remove(bot_id)
    gpu_runtime.on_session_ended(len(store.all_sessions()))
    runpod_runtime.on_session_ended(len(store.all_sessions()))
    return JSONResponse({"cancelled": True, "bot_id": bot_id})


@router.post("/sessions/{bot_id}/deliver")
async def deliver_artifact(bot_id: str, req: DeliverRequest, request: Request) -> JSONResponse:
    """Actually send the finished meeting's follow-up email + post it to Slack."""
    # Same machine-auth seam as /cancel: a PER-ORG bearer may deliver ONLY its
    # own org's artifacts; the global bearer keeps today's full service scope.
    machine_org = await run_in_threadpool(cedric.resolve_machine_org, request)
    if machine_org is None:
        if err := cedric.auth_error(request):  # CEDRIC
            return err
    artifact = await run_in_threadpool(
        store.get_artifact, bot_id, org_id=machine_org
    )
    # Wrong-org == not-found, byte-identical (no existence oracle for per-org
    # bearers). Adversarial review 2026-07-13, should-fix 2.
    if artifact is None or (
        machine_org is not None
        and str(artifact.get("org_id") or "") != machine_org
    ):
        return JSONResponse({"error": "no artifact for this bot_id"}, status_code=404)

    # Finalize already ran store.remove(bot_id), so store.get(bot_id) is None here
    # and the live session no longer carries the avatar identity. The saved
    # artifact does (avatar_id stamped at finalize) — resolve the follow-up's
    # name from there first, so a Cedric meeting's Slack header reads "Cedric"
    # and not the default "Laura". Fall back to the default only when absent
    # (pre-finalize / legacy artifacts) or on an unknown id.
    avatar_id = artifact.get("avatar_id") or settings.default_avatar_id
    try:
        name = avatars.load(avatar_id).name
    except Exception:  # noqa: BLE001 — an unknown avatar id must never block delivery
        name = avatars.load(settings.default_avatar_id).name
    email = artifact.get("follow_up_email", {}) or {}

    email_res = await run_in_threadpool(
        actions.send_email, req.to, email.get("subject", ""), email.get("body", "")
    )
    slack_res = {"sent": False, "reason": "disabled"}
    if req.slack:
        # CAPABILITY GATE: Slack delivery happens ONLY when this avatar's `slack`
        # toggle is on. Read raw and skip on an explicit OFF — an untouched
        # avatar keeps today's behaviour (default on when the org connected
        # Slack via Cedric). avatar_id comes from the saved artifact above.
        caps = await run_in_threadpool(store.get_avatar_capabilities, avatar_id)
        if caps.get("slack") is False:
            slack_res = {"sent": False, "reason": "slack capability off"}
        else:
            slack_res = await run_in_threadpool(
                actions.post_to_slack, actions.artifact_to_slack_text(name, artifact)
            )
    return JSONResponse({"email": email_res, "slack": slack_res})


@router.get("/sessions/{bot_id}/artifact")
def session_artifact(bot_id: str, request: Request) -> JSONResponse:
    """Retrieve a finished session's artifact (summary + checklist + email)."""
    # A PER-ORG bearer may read ONLY its own org's sessions/artifacts. Another
    # org's bot_id — live OR finalized — answers the IDENTICAL not-found body,
    # so the endpoint is never an existence/progress oracle (adversarial
    # review 2026-07-13, should-fix 2). The global bearer and the key-free
    # demo keep today's full service scope.
    machine_org = cedric.resolve_machine_org(request)
    if machine_org is None:
        if err := cedric.auth_error(request):  # CEDRIC
            return err
    org_scoped = machine_org is not None
    live = store.get(bot_id)
    if live is not None:
        if org_scoped and live.org_id != machine_org:
            return JSONResponse({"error": "unknown bot_id"}, status_code=404)
        return JSONResponse({"status": "in_progress", "bot_id": bot_id})
    artifact = store.get_artifact(bot_id, org_id=machine_org)
    if artifact is None or (
        org_scoped and str(artifact.get("org_id") or "") != machine_org
    ):
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    return JSONResponse({"status": "done", **cedric.wire_artifact(artifact)})  # CEDRIC: PII stays home


@router.post("/sessions/{bot_id}/redeliver")
async def redeliver_artifact(bot_id: str, request: Request) -> JSONResponse:
    """Retry a finished session's signed ``session.ended`` callback.

    Callback delivery is deliberately best-effort during meeting cleanup, so
    the stored artifact is the recovery source of truth.  This endpoint gives
    a logged-in owner (or the machine bearer) a bounded retry without ever
    sending the transcript across the PII boundary. A PER-ORG bearer may
    redeliver ONLY its own org's artifacts (never demo/unowned/another org's).
    """
    user = auth.current_user(request)
    token_org: Optional[str] = None
    if user is None:
        # Threadpooled: sync DB I/O (see _org_token_bearer_org's docstring).
        token_org = await run_in_threadpool(_org_token_bearer_org, request)
        if token_org is None:
            if err := auth.gate(request):
                return err

    lookup_org = str(user["org_id"]) if user is not None else token_org
    artifact = await run_in_threadpool(
        store.get_artifact, bot_id, org_id=lookup_org
    )
    if artifact is None:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    artifact_org = str(artifact.get("org_id") or "")
    # Own-org + legacy unowned ("") only — demo-org artifacts excluded for
    # logged-in users (self-serve product decision, 2026-07-13; same rule as
    # /sessions/{id}/end and dashboard.visible).
    if user is not None and artifact_org not in ("", user["org_id"]):
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    if token_org is not None and artifact_org != token_org:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)

    integration = cedric.default_integration()
    if not integration or not integration.get("callback_url"):
        return JSONResponse(
            {"error": "orchestrator callback is not configured"}, status_code=503
        )
    integration = {**integration, "org_id": artifact_org}
    # send_ended retries up to 4x with BLOCKING time.sleep (5s + 25s + 120s), so
    # awaiting it inline hangs the request ~150s and 504s at the proxy. Hand it to
    # the SAME fire-and-forget seam finalize uses (cedric.deliver_ended: distils
    # via wire_artifact, then schedules the retrying send off the request) and
    # answer 202 immediately. Delivery semantics are unchanged (still the full
    # retry chain, still the distilled/no-transcript payload) — only the blocking
    # of the HTTP request is removed.
    cedric.deliver_ended(integration, bot_id, artifact)
    return JSONResponse({"status": "retrying", "bot_id": bot_id}, status_code=202)
