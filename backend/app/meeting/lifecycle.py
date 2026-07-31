from __future__ import annotations
"""Meeting-lifecycle core hoisted from main.py: bot dispatch
(_start_avatar_session), post-meeting finalize (_finalize_session/_locked), and
their meter/usage/recall/artifact helper closure. api/ route groups share it
without importing main (circular). main.py re-imports the names its stayers
call — identity preserved; _finalizing/_graphiti_tasks/_gmail_state are
mutate-only shared state (never rebind).
"""
import asyncio, re, time, traceback, uuid
from datetime import datetime, timezone
from typing import Optional
import httpx
from fastapi.concurrency import run_in_threadpool
from ..config import settings
from .. import (anam_client, asana_client, autopilot, avatar_resolver, avatars,
                cedric, control_plane, drive_client, entitlements, executor,
                gemini_ears, google_client, gpu_runtime, graphiti_client,
                jira_client, ledger, llm, outbox, recall_client, runpod_runtime,
                store, tool_registry, tools)
from . import meeting_state
from ..meeting_state import build_from_utterances
from ..brain.engine import (post_meeting, degraded_post_meeting, type_actions,
                            prefill_summary_emails, headline_actions,
                            semantic_action_duplicates)
from ..decision import detect_browse_intent
from ..memory import meeting_memory
from ..openclaw import gates as openclaw_gates
from ..openclaw import runtime as openclaw_runtime


_BOT_TERMINAL = {"call_ended", "done", "fatal"}

# Strong refs for fire-and-forget memory tasks (compaction) — an unreferenced
# asyncio task can be garbage-collected before it runs.
_memory_tasks: set = set()


_finalizing: set[str] = set()


_BOT_VARIANT_RANK = {
    "web_gpu": 0,
    "web_4_core": 1,
    "web": 3,
}


def _stamp_action_routing(actions: list, org_id: str = "") -> list:
    """Routing-role stamps (agreed action-lifecycle contract
    hsk_con_cnw4567mqj3p49dyn3dg): every canonical action carries an
    IMMUTABLE execution_route decided here (native iff our executor will run
    its typed spec; else cedric), a correlation_id (defaults to action_id —
    the cross-log join), an execution_policy, and — when no owner was
    resolved — the visible triage payload (unresolved_roles +
    unassigned_reason) instead of a silent blank. Stamps are setdefault-only:
    a route persisted earlier is never re-evaluated (contract re-route bounds)."""
    out: list = []
    for a in actions or []:
        if not isinstance(a, dict):
            out.append(a)
            continue
        a = dict(a)
        if not a.get("execution_route"):
            a["execution_route"] = executor.route_for_typed(
                a.get("typed"), org_id,
                item_text=str(a.get("item") or a.get("action") or ""),
            )
        a.setdefault("correlation_id", str(a.get("action_id") or ""))
        a.setdefault("execution_policy", "approval_required")
        owner = str(a.get("owner") or "").strip()
        if not owner or owner.upper() == "UNASSIGNED":
            a.setdefault("unresolved_roles", ["owner"])
            a.setdefault("unassigned_reason", "no_owner_rule_match")
        out.append(a)
    return out


def _avatar_asana_enabled(org_id: str, avatar_id: str) -> bool:
    """Whether this avatar may use the org's Asana: the org is connected
    (per-org token or ASANA_TOKEN) AND this avatar is purpose-built for Asana
    (declares it in avatar.yaml — Petra does, other avatars don't) AND the
    per-avatar `asana` toggle is not explicitly off. So Asana defaults ON for
    PETRA ONLY, not every avatar whose org happens to have connected it; a
    dashboard toggle can still override per avatar. Sync (sqlite/yaml, both
    cached) — call via threadpool. Best-effort: never breaks a join/finalize."""
    try:
        # "Connected" now counts a Pipedream-brokered Asana account too, so the
        # native connection can be dropped once actions run through Pipedream.
        # Native check first (a fast local read); the Pipedream probe (cached,
        # best-effort) only runs when native is absent.
        from .. import pipedream_executor  # lazy: avoid load-order coupling

        if not (asana_client.connected(org_id)
                or pipedream_executor.app_connected(org_id, "asana")):
            return False
        declares = avatars.load(avatar_id).uses_native_tool("asana")
        return store.capability_enabled(
            avatar_id, "asana", connected=declares, org_id=org_id
        )
    except Exception:  # noqa: BLE001
        return False


def _avatar_pd_apps(avatar_id: str, org_id: str = "") -> dict:
    """The generic Pipedream apps this avatar may use, with each app's
    pre-built action catalog: {slug: [{key, name}]} — the offer type_actions
    presents to the model. An app qualifies only when the OWNER explicitly
    toggled it ON for this avatar (generic apps are opt-in; the approve door
    enforces the same rule via executor.capability_blocked). Capped to a few
    apps so the typing prompt stays small. Sync — call via threadpool.
    Best-effort: {} on any failure, never breaks finalize."""
    try:
        from .. import pipedream_client, pipedream_executor  # lazy

        if not pipedream_executor.enabled():
            return {}
        caps = store.get_avatar_capabilities(avatar_id, org_id)
        slugs = sorted(
            k for k, v in caps.items()
            if v and k not in ("google", "slack", "asana")
        )[:4]
        out: dict = {}
        for slug in slugs:
            try:
                catalog = pipedream_client.list_actions(slug, limit=15)
            except Exception:  # noqa: BLE001 — one app cannot hide the others
                catalog = []
            if catalog:
                out[slug] = catalog
        return out
    except Exception:  # noqa: BLE001
        return {}


def _bot_meeting_key(bot: dict) -> str:
    """Platform-aware meeting_key for a Recall bot record, to compare for
    EQUALITY against ledger.meeting_key(our_url) — not a Meet-only substring.

    Recall reports the joined meeting either as a full URL string or as a
    structured object carrying the platform-native meeting_id. ledger.meeting_key
    extracts exactly that native id from a URL (Meet code / Zoom id / Teams
    thread), so:
      - a URL string → normalize it the same way we normalized ours;
      - an object → its meeting_id IS the native id ledger.meeting_key produces,
        so compare on it directly (lower-cased).
    The old Meet-only regex made ``code`` the whole URL for Zoom/Teams while
    Recall reports an opaque id, so the substring test never matched and both
    durable guards silently no-op'd — two bots, two meters, uncleaned.
    """
    mu = bot.get("meeting_url")
    if isinstance(mu, dict):
        return str(mu.get("meeting_id") or "").strip().lower()
    return ledger.meeting_key(str(mu or ""))


def _recall_list_headers() -> dict[str, str]:
    return {
        "Authorization": settings.recall_api_key.strip(),
        "Content-Type": "application/json",
    }


def _bot_variant_rank(bot: dict) -> int:
    """Lower is better: GPU, then 4-core, then unknown paid variants, then default."""
    variant = bot.get("variant") or {}
    if isinstance(variant, dict):
        values = [str(v) for v in variant.values() if v]
    else:
        values = [str(variant)] if variant else []
    if not values:
        return _BOT_VARIANT_RANK["web"]
    return min(_BOT_VARIANT_RANK.get(v, 2) for v in values)


def _bot_status_code(bot: dict) -> str | None:
    """Latest Recall status_changes code (done/call_ended/in_call_recording/…)."""
    return (bot.get("status_changes") or [{}])[-1].get("code")


def _status_change_epoch(
    bot: dict, codes: set[str], *, first: bool = True
) -> float | None:
    """Epoch of the first (or last) status_changes entry whose code is in
    ``codes``. Recall stamps every status with its own created_at — the
    authoritative record of when the meter actually started/stopped, immune
    to our own polling lag. None when absent or unparsable."""
    changes = bot.get("status_changes") or []
    for ch in changes if first else reversed(changes):
        if ch.get("code") in codes:
            ts = str(ch.get("created_at") or "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                return None
            # A timestamp with no offset would otherwise be read as LOCAL time —
            # off by the host's UTC offset (hours of phantom consumed_seconds).
            # Recall stamps UTC; treat a naive value as UTC.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    return None


async def _close_usage_for(
    org_id: str, bot_id: str, end_epoch: float | None, reason: str
) -> None:
    """Close the durable usage row for a finished bot (PR B). Idempotent —
    entitlements.close_usage's ``WHERE state != 'closed'`` means the FIRST
    close wins, so manual end + webhook + reconcile retries can never rewrite
    consumed_seconds. consumed = end - in_call_at, where ``end`` is Recall's
    terminal-status timestamp when the caller had one, else now CAPPED at the
    deadline (our own enforcement lag is never billed to the org). A row that
    never went in-call closes 0 'never_joined'. Best-effort: a failure leaves
    the row open for the reconcile restore pass — it must NEVER break
    finalize. No-op in the key-free demo (control plane disabled)."""
    if not control_plane.enabled():
        return
    try:
        row = await run_in_threadpool(entitlements.usage_row, org_id, bot_id)
        if row is None or row.get("state") == "closed":
            return
        in_call = row.get("in_call_at")
        if in_call is None:
            await run_in_threadpool(
                entitlements.close_usage, org_id, bot_id, 0, "never_joined"
            )
            return
        end = end_epoch
        if end is None:
            end = time.time()
            if row.get("deadline"):
                end = min(end, float(row["deadline"]))
        consumed = max(0, int(round(end - in_call)))
        await run_in_threadpool(
            entitlements.close_usage, org_id, bot_id, consumed, reason
        )
    except Exception as e:  # noqa: BLE001 — reconcile's restore pass heals it
        print(
            f"[usage] close failed for bot={bot_id} ({type(e).__name__}); "
            f"reconcile will heal",
            flush=True,
        )


async def _assign_usage_bot_id(
    org_id: str, provisional_bot_id: str, real_bot_id: str
) -> bool:
    """Swap the gate's provisional usage bot_id ('pending:<uuid>') for the real
    Recall id, RETRYING a few times on a transient DB blip (PR B BLOCKER 1: a
    single swallowed swap failure left the usage row stranded under the
    provisional id → the live bot ran untracked → unmetered forever). If every
    attempt fails, the caller stops the born bot and fails closed. The reconcile
    self-heal remains a backstop when a leave cannot be verified immediately."""
    for attempt in range(3):
        try:
            changed = await run_in_threadpool(
                entitlements.assign_bot_id, org_id, provisional_bot_id, real_bot_id
            )
            if changed:
                return True
            # False is NOT success: retry, then force the caller down the
            # fail-closed cleanup path instead of running an unmetered bot.
        except Exception:  # noqa: BLE001 — transient billing-DB blip; retry
            if attempt == 2:
                print(
                    "[usage] provisional bot_id swap failed after retries — "
                    "reconcile self-heals",
                    flush=True,
                )
                return False
            await asyncio.sleep(0.2 * (attempt + 1))
    return False


async def _abandon_orphan_session(bot_id: str) -> None:
    """Drop a local session whose Recall bot no longer exists and that never
    captured anything — a cancelled or no-show scheduled bot. Stops the Anam
    conversation if one was opened (best-effort; the Recall bot is already gone,
    so no Recall meter remains), then removes the session. NO artifact and NO
    session.ended: the meeting never happened, so the orchestrator must not hear
    it 'ended' (that would be a phantom completed-meeting signal).

    Runs under the same ``_finalizing`` guard as ``_finalize_session`` so it
    can't race a concurrent finalize of the same bot: if a real finalize already
    holds the guard it owns this bot and we no-op; otherwise we hold it across the
    Anam teardown + remove so no finalize slips past ``store.remove``."""
    session = store.get(bot_id)
    if session is None or bot_id in _finalizing:
        return
    _finalizing.add(bot_id)
    try:
        if session.anam_conversation_id:
            try:
                await run_in_threadpool(
                    anam_client.end_conversation, session.anam_conversation_id
                )
            except Exception:
                pass
        # PR B: a no-show bot consumed nothing — release its usage row so the
        # org's one-active-meeting slot frees up (idempotent; never raises).
        await _close_usage_for(session.org_id, bot_id, None, "never_joined")
        store.remove(bot_id)
    finally:
        _finalizing.discard(bot_id)


_LEAVE_GONE_STATUSES = {404, 410}


def _leave_confirmed_stopped(exc: BaseException | None) -> bool:
    """True iff the Recall meter is CONFIRMED not billing: leave_call succeeded
    (exc is None) or Recall reports the bot genuinely gone (404/410). Every other
    error — 401/403/429 auth/rate-limit, 5xx, network/other — is UNVERIFIED, so
    the caller keeps the session for a retry rather than dropping a still-live,
    still-billing bot (the fleet-wide meter-leak class this whole change closes)."""
    if exc is None:
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _LEAVE_GONE_STATUSES
    return False


async def _bot_reports_terminal(bot_id: str) -> bool:
    """Best-effort status poll: True iff Recall AFFIRMATIVELY confirms the bot is
    not billing — its latest status is terminal (done/call_ended/fatal) or Recall
    no longer knows it (404/410 → gone). Used to DRAIN a leave_pending session
    whose leave_call keeps being rejected with a non-gone status (e.g. Recall's
    400 "bot is not in a call" for an already-ended bot): leave_pending IS a
    persisted field, so a phantom survives a redeploy (SQLite → S3 restore) and
    would otherwise be retried every reconcile pass FOREVER, inflating
    active_sessions. Any ambiguity — a still-live/non-terminal status, a non-200
    that isn't a gone-status, or a poll error — returns False so the caller KEEPS
    the session (never drop a possibly-live, possibly-billing bot)."""
    try:
        r = await run_in_threadpool(
            lambda: httpx.get(
                f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/{bot_id}/",
                headers=_recall_list_headers(),
                timeout=20.0,
            )
        )
    except Exception:  # noqa: BLE001 — a poll error keeps the session (retry next pass)
        return False
    if r.status_code in _LEAVE_GONE_STATUSES:
        return True  # Recall no longer has the bot → gone → not billing
    if r.status_code != 200:
        return False  # auth/rate/5xx — unknown, keep the session
    try:
        return _bot_status_code(r.json()) in _BOT_TERMINAL
    except Exception:  # noqa: BLE001 — unparseable body → treat as unknown
        return False


async def _retry_leave(bot_id: str, session: store.Session) -> bool:
    """Retry ONLY the Recall meter-stop for a session whose artifact was already
    built + delivered but whose leave_call could not be confirmed (so it was kept
    in the store for this retry — see _finalize_session_locked). Drop the session
    and signal the GPU meter when the meter is CONFIRMED stopped (leave succeeded
    OR the bot is gone 404/410 — otherwise a legitimate already-ended bot would
    be retried every pass FOREVER, inflating active_sessions and defeating the
    pre-deploy gate). NO artifact rebuild, NO re-delivery. Returns False while the
    stop is still UNVERIFIED so the reconcile loop retries next pass.

    Guarded by ``_finalizing`` so a manual /end and a reconcile tick can't both
    retry the same bot at once (idempotent regardless — defense in depth)."""
    if bot_id in _finalizing:
        return False
    _finalizing.add(bot_id)
    try:
        try:
            await run_in_threadpool(recall_client.leave_call, bot_id)
        except Exception as e:  # noqa: BLE001 — classified below
            if not _leave_confirmed_stopped(e):
                # The leave itself didn't confirm the stop. But a bot Recall now
                # reports terminal/gone is not billing — an already-ended bot
                # rejects the courtesy leave with 400 "not in a call", which is
                # NOT a gone-status, so without this a restored leave_pending
                # phantom (leave_pending is persisted → survives redeploy) would
                # retry forever. Confirm via a status poll before giving up.
                if not await _bot_reports_terminal(bot_id):
                    return False  # UNVERIFIED and not terminal — keep, retry next pass
            # 404/410 leave, OR Recall confirms terminal/gone → not billing → drop.
        # PR B BLOCKER 2: the meter is NOW confirmed off — close the usage row
        # here (finalize deferred it on the unverified leave to keep the slot
        # held). First-close-wins, so a manual /end + a reconcile tick racing
        # this both resolve to one honest close. consumed = confirmed-stop
        # moment (now, capped at the deadline); reason carried from finalize.
        await _close_usage_for(
            session.org_id,
            bot_id,
            getattr(session, "usage_end_epoch", None),
            getattr(session, "usage_close_reason", "") or "ended",
        )
        store.remove(bot_id)
        gpu_runtime.on_session_ended(len(store.all_sessions()))
        runpod_runtime.on_session_ended(len(store.all_sessions()))
        return True
    finally:
        _finalizing.discard(bot_id)


_graphiti_tasks: set = set()


async def _start_avatar_session(
    meeting_url: str,
    avatar_id: str = "",
    join_at: Optional[str] = None,
    integration: Optional[dict] = None,
    org_id: str = settings.demo_org_id,
    principal_id: str = "",
) -> dict:
    """Send a Recall bot (rendering the avatar page as its camera) into a meeting.

    Shared by the manual /sessions/start endpoint, the calendar auto-join webhook,
    and the Gmail watcher. Raises on failure. The avatar page mints its own fresh
    Anam token at render time and keys its websocket on the conversation_id.
    org_id is the owning tenant when a logged-in user dispatched (auth.py);
    the Demo org for service starts (Cedric, calendar auto-join, Gmail watcher).
    """
    # Avatar SELECTION precedence (M2, resolver-owned): explicit request >
    # user assignment > org default assignment > settings.default_avatar_id.
    # With overlays off this reduces to exactly the old one-liner.
    requested = await run_in_threadpool(
        lambda: avatar_resolver.resolve_avatar_key(
            org_id, requested=avatar_id, principal_id=principal_id
        )
    )
    requested = (requested or settings.default_avatar_id).strip()
    if avatars.is_internal(requested):
        # Internal personas are not dispatchable for ANY entry point (manual
        # start, calendar auto-join, Gmail watcher) — same error an unknown
        # folder raises, so callers treat it as a nonexistent avatar.
        raise FileNotFoundError(f"No avatar '{requested}'")
    # ONE canonical resolution per session (M2): the org's published overlay
    # applied over the immutable repo avatar — persona/voice/face/tools all
    # flow from this object. Flag off / no overlay ⇒ the exact avatars.load
    # cached instance (byte-identical behavior).
    avatar = await run_in_threadpool(
        avatar_resolver.resolve_for_dispatch, org_id, requested
    )  # raises if unknown
    conversation_id = uuid.uuid4().hex
    # avatar.page: per-avatar face tier (3D "talk" vs photoreal), falling back
    # to the global AVATAR_PAGE — the dashboard's "choose your avatar" knob.
    avatar_url = (
        f"{settings.public_base_url.rstrip('/')}/{avatar.page.strip('/')}"
        f"?avatar_id={avatar.id}&conversation_id={conversation_id}"
        f"&body={avatar.talk_body}&face_fallback={avatar.face_fallback}"
    )
    # ── entitlement gate (PR B) — THE single choke point for paid bots ──
    # Every entry point (manual start, calendar auto-join, Gmail watcher,
    # Cedric dispatch) funnels through here, so gating BEFORE create_bot is
    # gating everywhere. Only when the durable control plane is configured;
    # the key-free demo skips this entirely (byte-identical behaviour). The
    # usage row is inserted under a PROVISIONAL bot_id (the real id doesn't
    # exist until Recall answers) and swapped to the real one right after —
    # both inside the caller's per-meeting lock. open_usage raising
    # EntitlementsUnavailable (billing DB outage) or UsageDenied propagates
    # to the caller BEFORE any vendor dispatch: fail closed, never free.
    usage_bot_id = ""
    if control_plane.enabled():
        usage_bot_id = f"pending:{uuid.uuid4().hex}"
        gate = await run_in_threadpool(
            entitlements.open_usage, org_id, usage_bot_id, avatar.id
        )
        if gate is not None and not gate.get("ok"):
            raise entitlements.UsageDenied(
                gate.get("reason") or "usage_limit_reached"
            )
    try:
        bot = await run_in_threadpool(
            recall_client.create_bot, meeting_url, avatar_url, join_at, avatar.name,
            avatar.id,
        )
    except Exception:
        # No bot was born — release the pending usage row (consumes 0) so the
        # org's one-active-meeting slot isn't stranded by a failed dispatch.
        if usage_bot_id:
            try:
                await run_in_threadpool(
                    entitlements.close_usage, org_id, usage_bot_id, 0, "dispatch_failed"
                )
            except Exception:  # noqa: BLE001 — reconcile's restore heals orphans
                print("[usage] dispatch_failed close deferred to reconcile", flush=True)
        raise
    realtime_capability = str(bot.pop("_laura_realtime_capability", "") or "")
    session = store.create(
        bot_id=bot["id"], meeting_url=meeting_url, avatar_id=avatar.id,
        org_id=org_id, principal_id=principal_id,
    )
    # Stash the resolved avatar for the live path (frozen for the session,
    # exactly like mission): hot-path readers use avatar_resolver.for_session
    # — the stash or the canonical mtime-cached load, never database I/O.
    # Only an APPLIED overlay is stashed, so flag-off sessions keep the
    # canonical load()'s mid-meeting avatar.yaml refresh behavior.
    if getattr(avatar, "overlay_version", 0):
        session.resolved_avatar = avatar
    if usage_bot_id and not await _assign_usage_bot_id(
        org_id, usage_bot_id, bot["id"]
    ):
        # A born bot without a durable usage binding is never returned to the
        # customer. The hardened finalize attempts an immediate verified leave;
        # if Recall is temporarily unreachable the kept local session lets the
        # reconcile pass repair the provisional row and retry the stop.
        await _finalize_session(
            bot["id"], source="usage_binding_failed",
            usage_reason="usage_binding_failed",
        )
        if store.get(bot["id"]) is None:
            await run_in_threadpool(
                entitlements.close_usage,
                org_id, usage_bot_id, 0, "usage_binding_failed",
            )
        raise entitlements.EntitlementsUnavailable("usage_bot_binding_failed")
    if realtime_capability and not store.register_recall_realtime_capability(
        bot["id"], realtime_capability
    ):
        # Never leave a paid bot alive if its inbound realtime channel cannot
        # be authenticated.
        try:
            await run_in_threadpool(recall_client.leave_call, bot["id"])
        finally:
            store.remove(bot["id"])
        raise RuntimeError("could not secure Recall realtime endpoint")
    # CEDRIC: a summon that didn't carry its own wiring (the Gmail auto-join
    # watcher passes integration=None) still gets the Model A default routing —
    # otherwise an email-summoned meeting silently falls to Model B (no context
    # pull, no session.ended to Cedric). POST /sessions/start always passes its
    # own build_integration result, so it keeps winning; default_integration()
    # returns None when no SURFACE_* is set, leaving plain deployments unchanged.
    if integration is None:
        integration = cedric.default_integration()
    if integration:
        # Tenancy on the wire: every callback event carries the owning org so
        # the orchestrator can resolve the tenant even when external_ref is
        # empty (dashboard/email summons) — and the sender can pick a per-org
        # signing secret. "" for service starts keeps today's behaviour.
        session.integration = {**integration, "org_id": org_id}
    session.anam_conversation_id = conversation_id
    store.register_conversation(conversation_id, bot["id"], org_id=org_id)
    # Session-start briefs — five independent, best-effort reads gathered
    # CONCURRENTLY (they were serial; each is threadpool + cached + "" on any
    # failure, and none is on the live path, but bot dispatch shouldn't pay
    # their straight-line sum):
    #   carryover  — what previous sessions of this meeting link left open
    #   drive      — the avatar's shared folder (avatar.yaml drive_folder_id)
    #   asana      — workspace snapshot, when connected + avatar-enabled
    #   registry   — org-scoped tool context (feeds list_capabilities /
    #                search_tools with zero network in-meeting)
    #   calendar   — the owner org's upcoming meetings (feeds the
    #                upcoming_meetings brain tool, zero network in-meeting)
    async def _quiet(coro):
        try:
            return await coro
        except Exception:  # noqa: BLE001 — best-effort: the join never fails on a brief
            return None

    def _asana_brief_sync() -> str:
        # Gate + fetch in one threadpool hop (both are sync); TTL-cached in
        # asana_client, best-effort exactly like the Drive brief.
        if not _avatar_asana_enabled(org_id, avatar.id):
            return ""
        return asana_client.workspace_brief(org_id) or ""

    def _asana_live_sync() -> bool:
        # Whether the LIVE asana_* read tools are offered this session
        # (tools.specs_for): connected org + Asana-enabled avatar. Computed
        # once here — specs_for runs on the live path and must never touch
        # the DB.
        return _avatar_asana_enabled(org_id, avatar.id) and asana_client.connected(
            org_id
        )

    def _jira_brief_sync() -> str:
        # Jira open-issues snapshot for the PM avatar's grounding, mirroring the
        # Asana brief. Org-level gate (connected); TTL-cached in jira_client.
        if not jira_client.connected(org_id):
            return ""
        return jira_client.workspace_brief(org_id) or ""

    (carryover, folder, asana_snapshot, reg, cal_brief, asana_live,
     jira_snapshot, inbox_snapshot, week, cal_people) = await asyncio.gather(
        _quiet(run_in_threadpool(ledger.carryover_brief, meeting_url, org_id=org_id)),
        _quiet(
            run_in_threadpool(drive_client.folder_brief, avatar.drive_folder_id, org_id)
        )
        if avatar.drive_folder_id
        else _quiet(asyncio.sleep(0)),
        _quiet(run_in_threadpool(_asana_brief_sync)),
        _quiet(run_in_threadpool(tool_registry.assemble, org_id, avatar)),
        _quiet(run_in_threadpool(google_client.calendar_brief, org_id)),
        _quiet(run_in_threadpool(_asana_live_sync)),
        _quiet(run_in_threadpool(_jira_brief_sync)),
        # Inbox headers (from/subject/unread — never bodies): "what's on my
        # inbox?" had NO read path at all (owner 2026-07-24). Same contract as
        # the other briefs: TTL-cached, best-effort, the join never waits
        # beyond the slowest gather leg.
        _quiet(run_in_threadpool(google_client.gmail_inbox_brief, org_id)),
        # week — the accumulated past-7-days memory digest (Meeting Memory
        # Slice 1). Usually a single-row cache read; a regeneration is one
        # fast-model call bounded by the timeout so a slow model can never
        # delay bot dispatch (the whole entry stays best-effort via _quiet).
        _quiet(
            asyncio.wait_for(
                run_in_threadpool(meeting_memory.week_brief, org_id, avatar.id),
                timeout=settings.meeting_memory_brief_timeout_seconds,
            )
        ),
        # cal_people — org calendar contacts ({email, name, meeting_key}) for
        # the meeting-memory person graph + person_lookup. Cached alongside
        # calendar_brief; never rides the prompt itself.
        _quiet(run_in_threadpool(google_client.calendar_people, org_id)),
    )
    session.asana_live = bool(asana_live)
    # Distinct from asana_live (live READ TOOLS = native token + enablement):
    # whether a workspace brief actually loaded into her grounding. A
    # Pipedream-only org gets the brief but no live tools, and the capability
    # answer kept saying "no snapshot loaded" while she was reading tasks from
    # it (live 2026-07-24) — capabilities.py keys on THIS flag now.
    session.asana_brief_loaded = bool(asana_snapshot)
    # Keep the RAW board text on the session. memory_brief (below) is consumed
    # only by the legacy brain — an ElevenLabs-Agent session returns long before
    # it (main.py `_el_voice_owned`), so without this the avatar on that runtime
    # has no board at all while three other surfaces still tell her she has one.
    # api/voice_agent.build_init_payload reads this into the per-call prompt.
    session.asana_snapshot = asana_snapshot or ""
    # Diagnostic: an empty board is silent today — the avatar simply says "no
    # snapshot loaded" mid-meeting and nobody can tell which of the three gates
    # (org connected / avatar declares asana / per-avatar toggle) dropped it.
    # Counts only: the board itself rides the prompt, never the log.
    print(
        f"[asana] brief avatar={avatar.id} loaded={bool(asana_snapshot)} "
        f"chars={len(asana_snapshot or '')} live_tools={bool(asana_live)}",
        flush=True,
    )
    session.memory_brief = carryover or ""
    if folder:
        session.memory_brief = (
            f"[Shared Drive folder — current team docs]\n{folder}\n\n"
            + (session.memory_brief or "")
        )
    if asana_snapshot:
        # Honest label: this is the state at meeting START. When the live
        # asana_* tools are on, say so — that's what makes her READ current
        # state instead of quoting a stale snapshot.
        _asana_note = (
            " — use asana_projects / asana_tasks / asana_search for the CURRENT state"
            if asana_live
            else ""
        )
        session.memory_brief = (
            f"[Asana workspace — snapshot from meeting start{_asana_note}]\n"
            f"{asana_snapshot}\n\n" + (session.memory_brief or "")
        )
    if jira_snapshot:
        session.memory_brief = (
            "[Jira — open issues snapshot from meeting start]\n"
            f"{jira_snapshot}\n\n" + (session.memory_brief or "")
        )
    # Gmail inbox headers (never bodies) — "what's on my inbox?" answers from
    # this snapshot; the flag drives the deterministic capability answer.
    session.gmail_brief_loaded = bool(inbox_snapshot)
    if inbox_snapshot:
        session.memory_brief = (
            "[Gmail inbox — snapshot from meeting start (headers only)]\n"
            f"{inbox_snapshot}\n\n" + (session.memory_brief or "")
        )
    # Feed the workspace snapshot(s) into the org's knowledge graph (graphiti,
    # optional/off by default). Off the hot path, best-effort; strong-ref'd task.
    # (KEEP — merges keep reverting this wiring.)
    _kg_src = "\n\n".join(s for s in (asana_snapshot, jira_snapshot) if s)
    if _kg_src and graphiti_client.enabled():
        _kg_task = asyncio.create_task(graphiti_client.ingest(org_id, _kg_src))
        _graphiti_tasks.add(_kg_task)
        _kg_task.add_done_callback(_graphiti_tasks.discard)
    if reg:
        session.tool_registry = reg
        tools_brief = tool_registry.brief(reg)
        if tools_brief:
            session.memory_brief = (
                tools_brief + "\n\n" + (session.memory_brief or "")
            )
    if cal_brief:
        session.calendar_brief = cal_brief
        session.memory_brief = (
            f"[Owner's calendar — upcoming meetings]\n{cal_brief}\n\n"
            + (session.memory_brief or "")
        )
    if week:
        # Prepended last so the freshest context (this week, across ALL
        # meeting links) reads first. Digest is hard-capped at
        # meeting_memory_digest_max_chars — this block rides in every turn.
        # Kept separately on the session too: the rolling-summary refresher
        # passes it as linking context (Slice 2), and a restart re-reads it
        # via cached_digest.
        session.week_digest = week
        session.memory_brief = (
            "[Last 7 days — what the company discussed and decided, "
            "distilled from past meetings with dates — this IS your memory "
            "of the last week; cite it with dates when asked what you "
            f"remember]\n{week}\n\n"
            + (session.memory_brief or "")
        )
    # Calendar contacts + the starting user: the identity sources the
    # meeting-memory person graph enriches from at deposit time.
    session.calendar_people = list(cal_people or [])
    if principal_id:
        try:
            starter = await run_in_threadpool(store.get_user, principal_id)
            if starter and starter.get("email"):
                session.calendar_people.append(
                    {
                        "email": str(starter["email"]).lower(),
                        "name": str(starter.get("name") or ""),
                        "meeting_key": "",
                    }
                )
        except Exception:  # noqa: BLE001 — identity harvest is best-effort
            pass
    if settings.autopilot_brief and session.memory_brief:
        # Autopilot: mail/Slack "what's still open from last time" to the
        # owner as the bot joins. Fire-and-forget — never delays the join.
        asyncio.create_task(
            run_in_threadpool(autopilot.maybe_send_brief, meeting_url, avatar.name)
        )
    # Photoreal only: wake the GPU box for this meeting (fire-and-forget; the
    # page runs on the static-portrait fallback until the stream comes up).
    gpu_runtime.on_session_started()
    runpod_runtime.on_session_started(avatar.page)
    return {
        "bot_id": bot["id"],
        "conversation_id": conversation_id,
        "avatar_page_url": avatar_url,
        "scheduled_for": join_at,
    }


def _norm_action_text(text: str) -> str:
    """Normalization for action-item dedupe (mirrors ledger._norm's intent)."""
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


_ACTION_STOP = frozenset("a an the to for of in on at by with and or please just".split())


def _content_tokens(text: str) -> frozenset:
    return frozenset(_norm_action_text(text).split()) - _ACTION_STOP


def _same_action(a: str, b: str) -> bool:
    """True when two action lines describe the SAME request — one content-token
    set is a subset of the other. Requires >=2 shared content tokens so a single
    shared verb ('send') never over-merges two distinct asks."""
    ta, tb = _content_tokens(a), _content_tokens(b)
    if len(ta) < 2 or len(tb) < 2:
        return False
    return ta <= tb or tb <= ta


_DEMO_BROWSE_RE = re.compile(
    r"\b(show|walk|give|guide|demonstrate|teach|mostra|fai|dai)\b"
    r".{0,45}?"
    r"\b(how to|what to click|step[\s-]?by[\s-]?step|a tour|the tour|un tour|"
    r"passo[\s-]?passo|come (si )?(crea|creare|fa|fare|usa|usare|invit|aggiung))\b",
    re.IGNORECASE | re.DOTALL)


def _is_live_browse_item(text: str) -> bool:
    """A captured/extracted item that is really a LIVE browser-tour request
    ('show me the Asana dashboard', 'fammi un tour di Asana') — the avatar
    does it in the meeting, so it must not land in the post-meeting to-dos.
    Matches both the first-person live ask (detect_browse_intent) and the
    summarizer's third-person 'Show <name> how to…' / 'Give <name> a tour…'
    rephrasing, which would otherwise slip through as a to-do."""
    t = text or ""
    try:
        if detect_browse_intent(t)[0]:
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(_DEMO_BROWSE_RE.search(t))


def _merge_action_items(
    queued: list, extracted: list, avatar_name: str = ""
) -> list:
    """Artifact actions[] = live-captured queue_action items first, then the
    summarizer's extraction, deduped on normalized item text. A live capture
    wins a collision — it is the wording the room actually asked for — and
    ledger.record_meeting dedupes again on insert, so double-merging is safe.

    Every returned action carries a stable ``action_id``: a live capture keeps
    the id assigned at capture time (the same one already sent on its
    action.requested webhook), and a summarizer-only action — which never fired
    a live event — gets a fresh id here. The id is what the orchestrator dedupes
    and resolves on.

    May make ONE model call (the cross-language net below) — finalize-only;
    callers on the event loop must run it in a threadpool."""
    merged: list = []
    seen: set[str] = set()
    for q in queued or []:
        text = (q.get("action") or "").strip()
        if _is_live_browse_item(text):
            continue  # live browser tour, not a to-do
        key = _norm_action_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        owner = (q.get("owner") or "").strip()
        merged.append(
            {
                "action_id": q.get("action_id") or ledger.new_action_id(),
                "item": text,
                "owner": owner or "UNASSIGNED",
                "deadline": (q.get("due") or "").strip(),
                "gap_type": "none" if owner else "owner",
                "requested_live": True,  # additive marker: asked out loud in-meeting
            }
        )
    live_items = list(merged)  # everything so far is a live capture
    extras: list[dict] = []  # summarizer actions that survived the overlap dedup
    for a in extracted or []:
        if _is_live_browse_item((a.get("item") or a.get("action") or "")):
            continue  # summarizer picked up a live browse ask — drop it
        text = a.get("item", "") if isinstance(a, dict) else str(a)
        key = _norm_action_text(text)
        if key in seen:
            continue
        # Semantic dedup against the live captures: the summarizer routinely
        # re-extracts an action the room already asked live, just rephrased (the
        # deadline split into its own field). Keep the LIVE entry — its action_id
        # already went out on action.requested — and fold in the summarizer's
        # structured owner/deadline where the live capture had none. (#84 dedup.)
        dup = next((m for m in live_items if _same_action(text, m["item"])), None)
        if dup is not None:
            _fold_into_live(dup, a)
            continue
        if key:
            seen.add(key)
        a = dict(a) if isinstance(a, dict) else {"item": text}
        a.setdefault("action_id", ledger.new_action_id())
        # Assistant-request vs human commitment (owner 2026-07-24: "Send
        # Cedric the demo" — participants talking to EACH OTHER — surfaced as
        # a Needs-details email card). A summarizer-extracted action that was
        # NOT a direct request to the avatar is a team follow-up: kept for the
        # record, never typed/executed/interrogated. Live captures are
        # assistant-requests by definition and never carry this flag.
        if not _assistant_request(a, avatar_name):
            a["human_followup"] = True
        merged.append(a)
        extras.append(a)
    # Cross-language net: word overlap can't see that the summarizer restated an
    # Italian live capture in English ("schedula un meeting di prova con Ben…" →
    # "Schedule a test meeting with Ben…"), and that slip shipped TWO approval
    # cards → double execution. The prompt-level prevention in brain.post_meeting
    # stops most of it at the source; this one cheap model call (stub: no-op;
    # failure: keeps both) catches whatever still got through. Same fold-and-drop
    # semantics as the overlap path: the live entry — whose action_id already
    # went out on action.requested — always wins.
    if live_items and extras:
        drop: set[int] = set()
        for xi, li in semantic_action_duplicates(
            [m["item"] for m in live_items],
            [(x.get("item") or "") for x in extras],
        ):
            if id(extras[xi]) in drop:
                continue  # model repeated an extracted index: first match wins
            _fold_into_live(live_items[li], extras[xi])
            drop.add(id(extras[xi]))
        if drop:
            merged = [m for m in merged if id(m) not in drop]
    return merged


def _assistant_request(a: dict, avatar_name: str = "") -> bool:
    """Was this summarizer-extracted action a DIRECT request to the avatar?

    Trust the model's per-action "assistant" flag when present (the prompt
    asks for it); otherwise fall back deterministically on the EVIDENCE — the
    verbatim spoken excerpt — never the distilled item: items are always
    imperatives ("Send Cedric the demo"), so they'd all read as asks. The
    avatar-name check uses THIS meeting's avatar only ("Send Cedric the link"
    named a human participant called Cédric, not the Cedric avatar,
    live 2026-07-24). No evidence of a direct ask → a human commitment,
    captured for the record only."""
    flag = a.get("assistant")
    if isinstance(flag, bool):
        return flag
    evidence = str(a.get("evidence") or "")
    name = (avatar_name or "").strip()
    if name and re.search(rf"\b{re.escape(name)}\b", evidence, re.IGNORECASE):
        return True
    if not evidence:
        return False  # no spoken proof of a direct ask → note, not a card
    from ..brain.engine import wants_action_capture

    return wants_action_capture(evidence)


def _fold_into_live(live: dict, extracted: object) -> None:
    """Absorb the summarizer's structured owner/deadline into the winning live
    capture (which often has neither — the room just spoke the ask)."""
    if not isinstance(extracted, dict):
        return
    if not live.get("deadline") and extracted.get("deadline"):
        live["deadline"] = extracted["deadline"]
    ow = (extracted.get("owner") or "").strip()
    if live.get("owner") in ("", "UNASSIGNED") and ow and ow != "UNASSIGNED":
        live["owner"], live["gap_type"] = ow, "none"


async def _finalize_session(
    bot_id: str,
    source: str = "manual",
    failed_code: str = "",
    usage_reason: str = "",
    usage_end_epoch: float | None = None,
    artifact_org_id: str | None = None,
    bot_terminal: bool = False,
) -> dict | None:
    """End a session once: stop both vendors, build + store the artifact.

    Idempotent AND concurrency-safe — safe to call from the manual endpoint, the
    terminal-status webhook, and the reconciliation loop, even simultaneously.
    Returns the stored artifact (or None) if the session is already gone.

    ``source`` (manual/webhook/reconcile) is recorded PII-safely for diagnosing
    which path finalized a meeting; it never affects behaviour. ``failed_code``
    ('fatal' when the join fatally failed) fires the Cedric join-failed
    notification ONCE, inside the guard — so a fatal seen by both the webhook and
    the poll notifies the orchestrator a single time, not twice.

    ``usage_reason``/``usage_end_epoch`` (PR B) shape the durable usage close:
    the reason recorded on the row ('limit_reached' for the entitlement stop;
    natural ends default to 'ended') and Recall's own terminal timestamp when
    the caller had one. The close itself is idempotent (first close wins), so
    the same bot finalizing via manual end + webhook + reconcile still writes
    consumed_seconds exactly once. artifact_org_id is accepted only from an
    already-authenticated endpoint; it enables an RLS-scoped idempotent read
    after process replacement without adding a global bot-id lookup.

    ``bot_terminal`` (set by the two callers that KNOW Recall already reports the
    bot terminal — the reconcile terminal-status poll and the account terminal
    webhook) means the meter is ALREADY stopped: a bot in done/call_ended/fatal
    is not in a call and cannot bill. The courtesy leave_call below may then be
    rejected by Recall (e.g. 400 "bot is not in a call" for an already-ended
    bot) — which must NOT be treated as an UNVERIFIED stop, or the session is
    kept as leave_pending and retried forever, inflating active_sessions (the
    phantom that blocks the pre-deploy gate). Only a leave for a possibly-still-
    live bot (manual /end, bot_terminal=False) requires a verified stop.
    """
    session = store.get(bot_id)
    if session is None:
        # Without a trusted org there is deliberately no global Postgres
        # bot-id lookup. Same-process internal idempotency still hits the warm
        # cache; authenticated archive endpoints pass their org explicitly.
        if artifact_org_id is not None:
            return await run_in_threadpool(
                store.get_artifact, bot_id, org_id=artifact_org_id
            )
        return store.get_artifact(bot_id)
    # A prior finalize already built + delivered this session's artifact but its
    # Recall meter-stop (leave_call) was not confirmed, so the session was KEPT
    # for retry (see _finalize_session_locked). Re-finalizing must NOT rebuild or
    # re-deliver — just retry the meter-stop and drop the session once confirmed
    # stopped. _retry_leave self-guards on _finalizing, so a manual /end and a
    # reconcile tick can't double-retry. Return the already-stored artifact so
    # /end still answers 200 with the deliverable.
    if getattr(session, "leave_pending", False):
        await _retry_leave(bot_id, session)
        return await run_in_threadpool(
            store.get_artifact, bot_id, org_id=session.org_id
        )
    # Concurrency guard. store.remove(bot_id) — the thing that makes the
    # `session is None` check above idempotent — only runs at the very END of
    # the body, past several awaits (the multi-second post_meeting LLM call
    # included). So two finalizes racing on the same bot (bot.call_ended +
    # bot.done arrive as concurrent webhook POSTs; the poll loop is a third
    # racer) would BOTH pass the None check and BOTH fire session.ended to
    # Cedric. Check-and-add is synchronous — no await between here and the add —
    # so it is atomic under asyncio's single-threaded loop.
    if bot_id in _finalizing:
        return await run_in_threadpool(
            store.get_artifact, bot_id, org_id=session.org_id
        )
    _finalizing.add(bot_id)
    try:
        return await _finalize_session_locked(
            bot_id, session, source, failed_code, usage_reason, usage_end_epoch,
            bot_terminal,
        )
    finally:
        _finalizing.discard(bot_id)


async def _finalize_session_locked(
    bot_id: str,
    session: store.Session,
    source: str,
    failed_code: str = "",
    usage_reason: str = "",
    usage_end_epoch: float | None = None,
    bot_terminal: bool = False,
) -> dict | None:
    """The actual finalize body, run under the ``_finalizing`` in-flight guard."""
    # Gemini ears: tear down this bot's audio session first (idempotent, sync).
    gemini_ears.stop_session(bot_id)
    # Join-failed notification (fatal): fire here, under the guard, so it runs at
    # most once per bot even when the webhook and the poll both observe the fatal.
    if failed_code == "fatal":
        cedric.notify_failed(session, bot_id, failed_code)  # CEDRIC
    transcript_text = session.transcript_text()
    # Raw transcript retains agent output for the archive. Intelligence,
    # decisions and actions use human evidence only.
    analysis_transcript_text = session.transcript_text(include_agents=False)

    # Stop billing on both vendors. leave_call is the Recall meter-stop and now
    # RAISES on a persistent failure (retry=True + raise_for_status). The stop is
    # only CONFIRMED when leave succeeds or Recall reports the bot genuinely gone
    # (404/410, e.g. a naturally-ended meeting); a 401/403 (rotated key), 429, or
    # 5xx leaves it UNVERIFIED — the bot may still be live+billing. When
    # unverified we keep the session below so the reconcile backstop retries the
    # leave; the artifact is still built + persisted + delivered here so the
    # deliverable is never lost.
    leave_verified = True
    try:
        await run_in_threadpool(recall_client.leave_call, bot_id)
    except Exception as e:  # noqa: BLE001 — classified by _leave_confirmed_stopped
        # A bot Recall ALREADY reports terminal (bot_terminal) is not billing —
        # leave_call is a courtesy and its rejection (e.g. 400 "not in a call"
        # for an already-ended bot) must not strand the session as leave_pending
        # (phantom active_sessions). A possibly-still-live bot still needs a
        # verified stop (404/410/success) — the fleet meter-leak guard.
        leave_verified = bot_terminal or _leave_confirmed_stopped(e)
    if session.anam_conversation_id:
        try:
            await run_in_threadpool(
                anam_client.end_conversation, session.anam_conversation_id
            )
        except Exception:
            pass

    # PR B BLOCKER 2: the usage close is DEFERRED to the verified-stop branch
    # below — closing here (before the leave is confirmed off) would free the
    # org's one-meeting slot while the bot may still be live+billing, letting a
    # 2nd meeting start against a still-running meter. On a VERIFIED stop we
    # close (first-close-wins); on an UNVERIFIED leave we stash the reason/end
    # and leave the row OPEN so the slot stays held until _retry_leave confirms
    # the meter is off and closes it. The resolved reason is computed once here.
    usage_close_reason = usage_reason or (
        "failed" if failed_code == "fatal" else "ended"
    )

    artifact: dict = {
        "summary": "",
        "decisions": [],
        "actions": [],
        "checklist": [],
        "missing_steps": [],
        "readiness_score": 0,
        "risks": [],
        "follow_up_email": {},
    }
    integration = dict(session.integration) if session.integration else None  # CEDRIC
    openclaw_active = openclaw_gates.experiment_enabled_for_org(session.org_id)
    # Live-captured action requests (tools.queue_action) — read once, used twice:
    # shown to the summarizer as "already captured, do not re-extract" (dedup
    # prevention at the source, cross-language included) and then merged into
    # the artifact's actions[] below.
    queued_actions = await run_in_threadpool(
        outbox.begin_action_finalize, session.org_id, bot_id
    )
    if not queued_actions:
        # Compatibility for synthetic/key-free sessions captured before the
        # durable queue existed.
        queued_actions = list(getattr(session, "queued_actions", None) or [])
    if analysis_transcript_text.strip():
        avatar = avatar_resolver.for_session(session)
        analysis_state = session.meeting_state
        if analysis_state is None:
            analysis_state = meeting_state.build_from_utterances(
                avatar, session.human_transcript()
            )
        try:
            artifact = await run_in_threadpool(
                lambda: post_meeting(
                    avatar,
                    analysis_transcript_text,
                    context=(integration or {}).get("brief", ""),  # CEDRIC: brief-grounded summary
                    live_actions=queued_actions,
                    state=analysis_state,
                )
            )
        except Exception as e:  # noqa: BLE001 — a transient post-model failure must
            # not 500 /end and lose the whole deliverable. post_meeting passes an
            # EXPLICIT provider, so llm.complete's 'never go dark' Haiku fallback
            # (gated on provider is None) does NOT run and a 429/529/timeout
            # re-raises up to here. Degrade to the deterministic tracker recap so
            # the artifact still saves + delivers below; the meter-stop already
            # ran above and the leave-retry path stays intact. (No transcript is
            # logged — PII: only the exception class name.)
            print(
                f"[finalize] bot={bot_id} post_meeting failed "
                f"({type(e).__name__}); degrading to deterministic recap",
                flush=True,
            )
            try:
                artifact = await run_in_threadpool(
                    degraded_post_meeting,
                    avatar,
                    analysis_transcript_text,
                    state=analysis_state,
                )
            except Exception as e2:  # noqa: BLE001 — the degraded rebuild ALSO failed.
                # This is no longer a transient LLM blip: degraded_post_meeting
                # re-runs build_from_text/_finish_artifact, so a real bug there
                # would re-raise and 500 /end + lose the artifact — the exact
                # failure Fix 3 exists to prevent. Save + deliver a bare
                # deterministic scaffold so _finalize_session_locked can NEVER
                # throw on the post-meeting build (the full transcript is still
                # persisted into the artifact below + served by the archive), and
                # SURFACE the stack so a genuine code bug is diagnosable instead of
                # silently degrading every meeting forever. PII-safe: a traceback
                # carries the exception + code frames, never transcript/recap text.
                print(
                    f"[finalize] bot={bot_id} degraded recap ALSO failed "
                    f"({type(e2).__name__}); saving bare scaffold\n"
                    f"{traceback.format_exc()}",
                    flush=True,
                )
                artifact = {
                    "summary": (
                        "Automated recap unavailable — the post-meeting summarizer "
                        "failed. The full transcript is preserved in the meeting "
                        "archive."
                    ),
                    "decisions": [],
                    "actions": [],
                    "checklist": [],
                    "missing_steps": [],
                    "readiness_score": 0,
                    "risks": [],
                    "follow_up_email": {},
                }

    # Fold the live captures into the artifact's actions[] ahead of the
    # summarizer's extraction, deduped on normalized item text. Runs BEFORE
    # save_artifact and ledger.record_meeting so every consumer — stored
    # artifact, wire artifact, ledger, autopilot — sees the same merged list.
    # Plain non-orchestrated sessions benefit too. Always run the merge (even
    # with no live captures) so EVERY action carries a stable action_id — the
    # summarizer-only actions get one here too. Threadpool because the merge's
    # cross-language net may make one model call: finalize is off the live path,
    # but the event loop (other meetings' live turns) must never wait on it.
    # Avatar display name for the assistant-vs-human classifier. `avatar` is
    # only bound above when there was analysis transcript — resolve defensively
    # (an empty-transcript finalize still merges live captures).
    _avatar_name = ""
    try:
        _avatar_name = avatar_resolver.for_session(session).name
    except Exception:  # noqa: BLE001 — classification degrades, finalize never breaks
        _avatar_name = str(getattr(session, "avatar_id", "") or "")
    artifact["actions"] = await run_in_threadpool(
        _merge_action_items, queued_actions, artifact.get("actions") or [],
        _avatar_name,
    )
    artifact["checklist"] = artifact["actions"]  # legacy alias, same list

    # Typed-action specs for the native executor (NATIVE-INTEGRATIONS-PLAN.md):
    # annotate each action with a {type, args} spec where it CLEARLY maps
    # (calendar.create_event / email.send — plus asana.create_task for an
    # avatar allowed to use the org's connected Asana) so an APPROVED action
    # can be run natively. GATED on the flag — with NATIVE_EXECUTOR off this
    # whole block is skipped, so finalize is byte-identical to today (no extra
    # model call, no new field). Never invents recipients/times; unmapped
    # actions stay generic. Best-effort: typing must never break finalize.
    if executor.enabled():
        try:
            allow_asana = await run_in_threadpool(
                _avatar_asana_enabled, session.org_id, session.avatar_id
            )
            pd_apps = await run_in_threadpool(
                _avatar_pd_apps, session.avatar_id, session.org_id
            )
            summary_brief = artifact.get("summary") or ""
            # "Email X the summary of this meeting" → Subject/Body prefilled
            # from the artifact's own summary BEFORE typing, so the email.send
            # card arrives ready instead of interrogating the human for a body
            # that already exists (live 2026-07-24).
            artifact["actions"] = await run_in_threadpool(
                lambda: type_actions(
                    prefill_summary_emails(artifact["actions"], summary_brief),
                    summary_brief,
                    allow_asana=allow_asana, pd_apps=pd_apps,
                )
            )
            artifact["checklist"] = artifact["actions"]
            # Find-a-time (SCHEDULER_FIND_TIME, off by default): a VAGUE
            # scheduling ask ("book 45 min with Ananth next week") is still
            # untyped after type_actions (no ISO time), so it would otherwise be
            # dropped. Attach ranked candidate slots (free/busy on the org's own
            # calendar + any attendee emails named in the ask) so the approve
            # doors can offer times. Runs AFTER type_actions so explicit-time
            # asks (already typed) are skipped. Best-effort + off the hot path;
            # any failure is caught by the same handler below (never fatal).
            if settings.scheduler_find_time:
                from .. import scheduler

                _org_oauth = await run_in_threadpool(
                    store.get_org_oauth, session.org_id
                )
                _org_email = str((_org_oauth or {}).get("email") or "")
                _tz = await run_in_threadpool(
                    google_client.resolve_timezone, session.org_id
                ) or "UTC"
                artifact["actions"] = await run_in_threadpool(
                    lambda: scheduler.enrich_actions(
                        artifact["actions"], summary_brief,
                        principal=session.org_id, organizer_email=_org_email,
                        timezone=_tz,
                    )
                )
                artifact["checklist"] = artifact["actions"]
            # Asana auto-push (ASANA_AUTO_EXECUTE, off by default): typed
            # tasks land on the board NOW instead of waiting for dashboard
            # approval; receipts (task URLs) go through the same provenance
            # channel as approved runs. Best-effort — finalize (the meter
            # stop) is already safe above and must never wait on Asana.
            if allow_asana and settings.asana_auto_execute and not openclaw_active:
                pushed = await run_in_threadpool(
                    executor.auto_execute_asana,
                    session.org_id,
                    artifact["actions"],
                )
                if pushed:
                    print(
                        f"[finalize] bot={bot_id} asana auto-push: "
                        f"{pushed} action(s)",
                        flush=True,
                    )
        except Exception as e:  # noqa: BLE001 — enrichment only, never fatal
            print(
                f"[finalize] bot={bot_id} typed-action producer skipped "
                f"({type(e).__name__})",
                flush=True,
            )

    # Gist headlines — a short imperative `title` per action so the Action
    # Centre reads as a task list, not raw transcript. Runs over the FINAL
    # merged list (live captures + model-extracted + goal-inferred) so every
    # card gets one. Best-effort + off the live path; a failure leaves titles
    # to the dashboard's deterministic _display_title fallback. Uses the
    # post-meeting provider (the quality model), never the fast live one.
    try:
        artifact["actions"] = await run_in_threadpool(
            lambda: headline_actions(
                artifact.get("actions") or [], artifact.get("summary") or ""
            )
        )
        artifact["checklist"] = artifact["actions"]
    except Exception as e:  # noqa: BLE001 — enrichment only, never fatal
        print(
            f"[finalize] bot={bot_id} headline producer skipped "
            f"({type(e).__name__})",
            flush=True,
        )

    # Routing-role stamps — UNCONDITIONAL (executor on or off): finalize is
    # the contract's first decision point, and the route persisted here is
    # what the approve door executes by.
    artifact["actions"] = _stamp_action_routing(
        artifact.get("actions") or [], session.org_id)
    artifact["checklist"] = artifact["actions"]

    # The transcript is the raw material of the artifact — persist it so the
    # product output is complete (transcript + summary + checklist + email).
    # It lives only in the artifact store (PII: never logged).
    artifact["transcript"] = transcript_text

    # Attribution metadata: the session row is deleted below (store.remove), so
    # the artifact is the only place that remembers WHICH avatar ran WHICH
    # meeting — the dashboard groups meetings per avatar from these fields.
    # Duration is approximated from first-to-last utterance timestamps (counts
    # only; no utterance text — the guard hook forbids logging transcript).
    artifact["avatar_id"] = session.avatar_id
    artifact["org_id"] = session.org_id
    # WHO sent the avatar in (dashboard user), "" for service/legacy starts —
    # the per-user archive scope reads this before falling back to
    # transcript attendance.
    artifact["principal_id"] = getattr(session, "principal_id", "")
    artifact["meeting_url"] = session.meeting_url
    if len(session.transcript) >= 2:
        artifact["duration_seconds"] = int(
            session.transcript[-1].ts - session.transcript[0].ts
        )

    # PRODUCTION DURABILITY ORDER: for orchestrated sessions, commit the
    # transcript-free session.ended envelope to Postgres BEFORE any local
    # artifact write, ledger write, or session cleanup. If Postgres is
    # configured but unavailable, OutboxUnavailable propagates and the local
    # session remains available for a retry; we never claim completion while
    # the only customer delivery record could still be lost.
    orchestrated = False
    if not openclaw_active:
        orchestrated = cedric.deliver_ended(integration, bot_id, artifact)  # CEDRIC

    await run_in_threadpool(
        store.save_artifact, bot_id, artifact, org_id=session.org_id
    )
    # Product analytics: one counter per finished meeting (ids + counts only,
    # NEVER content) — the last step of the site→signup→meeting funnel.
    try:
        from ..integrations import product_analytics

        product_analytics.capture(
            "meeting_finalized", session.org_id,
            {"avatar_id": session.avatar_id,
             "actions": len(artifact.get("actions") or [])},
        )
    except Exception:  # noqa: BLE001 — analytics never break finalize
        pass
    # Cross-meeting memory: fold this meeting's extracted facts into the
    # ledger. Best-effort — memory must never block the cleanup below
    # (session removal + GPU meter signal), so a ledger hiccup is swallowed.
    try:
        await run_in_threadpool(
            ledger.record_meeting, session.meeting_url, session.avatar_id, bot_id,
            artifact, org_id=session.org_id,
        )
    except Exception:
        pass
    # Meeting Memory (Slice 1): deposit the DISTILLED fields into the durable
    # memory tables while the roster is still on the session (participant rows
    # are deleted by store.remove below). Same best-effort rule as the ledger,
    # plus a hard bound: a HUNG control plane (vs an error) must not delay the
    # session-removal/GPU-meter cleanup below.
    try:
        await asyncio.wait_for(
            run_in_threadpool(meeting_memory.deposit, session, artifact),
            timeout=10.0,
        )
        # Bounded growth (Slice 3): compaction rides behind deposits — daily
        # per-org throttle inside maybe_compact, fire-and-forget, never on
        # the finalize response path. STRONG-REF'd: an unreferenced task can
        # be GC'd before it runs (the 2026-07-25 leave_meeting incident —
        # see api/voice_agent._leave_tasks).
        _compact_task = asyncio.create_task(
            run_in_threadpool(meeting_memory.maybe_compact, session.org_id)
        )
        _memory_tasks.add(_compact_task)
        _compact_task.add_done_callback(_memory_tasks.discard)
    except Exception:
        pass
    openclaw_started = False
    if openclaw_active:
        try:
            result = await run_in_threadpool(
                openclaw_runtime.create_meeting_run,
                bot_id,
                artifact,
                session.org_id,
            )
            openclaw_started = bool((result or {}).get("ok"))
        except Exception as e:  # noqa: BLE001 — OpenClaw never blocks cleanup
            print(
                f"[finalize] bot={bot_id} OpenClaw start skipped "
                f"({type(e).__name__})",
                flush=True,
            )
    if orchestrated or openclaw_active:
        pass  # external owner owns post-meeting delivery/execution
    elif settings.autopilot_deliver:
        # Autopilot: send the drafted follow-up + Slack summary now, without
        # holding up the finalize response (meter is already stopped above).
        # Best-effort like the ledger — never blocks the cleanup below.
        try:
            name = avatar_resolver.for_session_offpath(session).name
            asyncio.create_task(run_in_threadpool(autopilot.maybe_deliver, name, artifact))
        except Exception:
            pass
    # PII-safe finalize telemetry: counts + booleans only, NEVER any utterance
    # text (the guard hook enforces this). Lets the next live test see which
    # path finalized and whether content actually accumulated — the question
    # left open by the empty-artifact test run.
    n_lines = len(session.transcript)
    had_content = bool(transcript_text.strip())
    print(
        f"[finalize] bot={bot_id} source={source} lines={n_lines} "
        f"orchestrated={orchestrated} openclaw={openclaw_started} "
        f"content={had_content}",
        flush=True,
    )
    if leave_verified:
        # Meter CONFIRMED off → NOW close the durable usage row (first close
        # wins; idempotent across manual end + webhook + reconcile). Best-effort
        # — a billing hiccup leaves the row for the reconcile restore pass.
        await _close_usage_for(session.org_id, bot_id, usage_end_epoch, usage_close_reason)
        store.remove(bot_id)
        # Photoreal only: last session out turns off the GPU meter (after a grace
        # window, in case another meeting starts right away).
        gpu_runtime.on_session_ended(len(store.all_sessions()))
        runpod_runtime.on_session_ended(len(store.all_sessions()))
    else:
        # Meter-stop UNVERIFIED (Recall 5xx / network error): keep the session in
        # the store so the reconcile backstop retries leave_call — the poll only
        # revisits sessions still in the store, so removing it now would strand a
        # still-live bot billing forever. The artifact is already saved and
        # delivered above; the retry routes through leave_pending so it must NOT
        # re-deliver. (No transcript is logged — PII.)
        # PR B BLOCKER 2: DO NOT close the usage row here — the slot stays held
        # (a 2nd meeting for this org is correctly refused) until _retry_leave
        # confirms the meter is off. Stash the reason/end so that close is honest.
        session.leave_pending = True
        session.usage_close_reason = usage_close_reason
        session.usage_end_epoch = usage_end_epoch
        print(
            f"[finalize] bot={bot_id} leave_call unverified — session kept for "
            f"reconcile meter-stop retry",
            flush=True,
        )
    return artifact
