"""OAuth connect/callback flows — Google Calendar (Recall Calendar V2) and
Asana — extracted from main.py. Owner-gated when login is configured; the
key-free demo path (no credentials -> 400) is unchanged."""
import httpx
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.concurrency import run_in_threadpool

from app import auth, store, google_client, asana_client, recall_client, drive_client, gmail_watcher
from app.core.config import settings
from app.api.deps import _calendar_target_emails

router = APIRouter()

GOOGLE_CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events.readonly",
    # calendarList (which calendars the user displays) needs calendar.readonly —
    # calendar.events[.readonly] alone 403s users/me/calendarList, silently
    # degrading the all-calendars upcoming view to primary-only (calendars=1
    # in the [calendar] diagnostic). Existing connections must reconnect once
    # to grant it; until then the fan-out keeps its primary-only fallback.
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    # Read Laura's inbox so "Add people" invites (which email her a Meet link,
    # with no calendar event) can auto-join the meeting. See gmail_watcher.py.
    "https://www.googleapis.com/auth/gmail.readonly",
    # Read shared Drive folders so an avatar with drive_folder_id walks into
    # meetings knowing the team's docs. See drive_client.py. Adding a scope
    # means reconnecting once via /oauth/google/connect.
    "https://www.googleapis.com/auth/drive.readonly",
    # WRITE scopes for the native executor (NATIVE-INTEGRATIONS-PLAN.md): create
    # calendar events and send Gmail on the connected account. The Google consent
    # screen lists these; a user reconnects once via /oauth/google/connect to
    # grant them. Harmless to request even with native_executor off (the executor
    # is what actually uses them, and it stays gated by the flag).
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.send",
)


def _google_redirect_uri() -> str:
    return settings.google_calendar_redirect_uri.strip() or (
        f"{settings.public_base_url.rstrip('/')}/oauth/google/callback"
    )


# ── Google Calendar OAuth: connect Laura's calendar to Recall Calendar V2 ──
# Per-browser CSRF state for THIS flow. The nonce lives in an HttpOnly cookie
# scoped to /oauth (covers both connect and callback) and, signed, inside the
# `state` param — the callback requires both to match. CALENDAR_STATE_PURPOSE
# domain-separates the signature from the login flow's "state", so neither
# flow's state can be replayed in the other. This REPLACES the old static
# settings.calendar_oauth_state as the real CSRF barrier.
CALENDAR_STATE_COOKIE = "laura_calendar_oauth_state"
CALENDAR_STATE_PURPOSE = "calendar_state"


def _clear_calendar_state(response):
    """Consume the single-use OAuth-state cookie (mirrors auth.py's callback)."""
    response.delete_cookie(CALENDAR_STATE_COOKIE, path="/oauth")
    return response


def _oauth_login_required(request: Request):
    """FIX: when login is enabled, the native-Google connect/callback flow must
    be owner-authenticated — an anonymous browser must NOT be able to complete
    OAuth and silently land a Google refresh token on demo_org_id. Returns an
    error response to send, or None to allow. auth.enabled() is exactly "the
    Google OAuth client is configured", which is also what this flow needs, so
    the key-free demo path (no Google → these endpoints already 400) is left
    unchanged: the gate only bites once real credentials exist."""
    if auth.current_user(request) is not None:
        return None
    if not auth.enabled():
        return None  # key-free / no-login demo path, unchanged
    if err := auth.gate(request):
        return err
    # A valid machine bearer clears auth.gate with no cookie user; an interactive
    # OAuth connect still needs a real logged-in owner to key the token to.
    return JSONResponse({"error": "login required"}, status_code=401)


@router.get("/oauth/google/connect")
def google_oauth_connect(request: Request):
    """Start Google OAuth for the calendar account that should invite Laura."""
    if not settings.google_calendar_client_id:
        return JSONResponse(
            {"error": "GOOGLE_CALENDAR_CLIENT_ID is not set."}, status_code=400
        )
    if err := _oauth_login_required(request):
        return err

    # Per-session single-use CSRF state (see auth.issue_oauth_state): the nonce
    # goes in an HttpOnly cookie, the signed token in the `state` param.
    nonce, signed_state = auth.issue_oauth_state(CALENDAR_STATE_PURPOSE)
    params = {
        "client_id": settings.google_calendar_client_id,
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(GOOGLE_CALENDAR_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": signed_state,
    }
    # Default the calendar to the SAME Google account the user logged in with, so
    # "log in -> see your own calendar" just works and you can't accidentally
    # connect a different account's (empty) calendar. login_hint pre-selects it;
    # the user can still switch to another account on Google's own screen.
    _cu = auth.current_user(request)
    if _cu and _cu.get("email"):
        params["login_hint"] = _cu["email"]

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    resp = RedirectResponse(url)
    resp.set_cookie(
        CALENDAR_STATE_COOKIE,
        nonce,
        max_age=auth.STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.public_base_url.startswith("https"),
        path="/oauth",
    )
    return resp


@router.get("/oauth/google/callback")
async def google_oauth_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> JSONResponse:
    """Finish Google OAuth, then create the Recall calendar connection."""
    if error:
        return JSONResponse({"error": error}, status_code=400)
    if not code:
        return JSONResponse({"error": "Missing Google OAuth code."}, status_code=400)
    # FIX (auth gate): on a login-enabled deployment this callback must belong to
    # a logged-in owner, else an anonymous request could complete OAuth and store
    # a token on demo_org_id. Checked BEFORE the state so an unauth caller never
    # even reaches the token exchange.
    if err := _oauth_login_required(request):
        return err
    # FIX (CSRF): the returned `state` must match the single-use signed nonce we
    # set on THIS browser at /oauth/google/connect (cookie + signed param). This
    # binds the callback to the browser that started the flow — the real barrier
    # against login-CSRF / refresh-token injection, replacing the old static
    # settings.calendar_oauth_state gate. Consume the cookie on every exit below.
    cookie_nonce = request.cookies.get(CALENDAR_STATE_COOKIE, "")
    if not auth.check_oauth_state(state, cookie_nonce, CALENDAR_STATE_PURPOSE):
        return _clear_calendar_state(
            JSONResponse({"error": "Invalid OAuth state."}, status_code=400)
        )
    if not settings.google_calendar_client_id:
        return _clear_calendar_state(JSONResponse(
            {"error": "GOOGLE_CALENDAR_CLIENT_ID is not set."}, status_code=400
        ))
    if not settings.google_calendar_client_secret:
        return _clear_calendar_state(JSONResponse(
            {"error": "GOOGLE_CALENDAR_CLIENT_SECRET is not set."}, status_code=400
        ))

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.google_calendar_client_id,
                    "client_secret": settings.google_calendar_client_secret,
                    "redirect_uri": _google_redirect_uri(),
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            token = token_resp.json()

            refresh_token = token.get("refresh_token", "")
            if not refresh_token:
                return _clear_calendar_state(JSONResponse(
                    {
                        "error": (
                            "Google did not return a refresh_token. Re-open "
                            "/oauth/google/connect and approve with prompt=consent; "
                            "if needed, revoke the app in Google settings first."
                        )
                    },
                    status_code=400,
                ))

            oauth_email = ""
            access_token = token.get("access_token", "")
            if access_token:
                profile_resp = await client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if 200 <= profile_resp.status_code < 300:
                    oauth_email = profile_resp.json().get("email", "").lower()
    except httpx.HTTPStatusError as e:
        return _clear_calendar_state(JSONResponse(
            {"error": "Google OAuth token exchange failed.", "status": e.response.status_code},
            status_code=400,
        ))
    except Exception as e:
        return _clear_calendar_state(JSONResponse({"error": str(e)}, status_code=400))

    # PER-USER Google connect. This callback used to accept ONLY the avatar's own
    # inbox (CALENDAR_INVITE_EMAILS) and reject everyone else with "Wrong Google
    # account". Relaxed: ANY authenticated user may connect THEIR OWN Google
    # (Calendar + Gmail) — the per-org refresh token stored below powers their
    # native executor AND their own Upcoming calendar. The avatar-only Recall
    # calendar auto-join step (further down) stays guarded to the avatar account,
    # so a normal user's connect can never hijack the deployment's auto-join inbox.
    targets = _calendar_target_emails()
    # The avatar's own account. With NO invite filter configured this is a
    # single-tenant deployment where the connecting account IS the auto-join
    # calendar (preserves the pre-per-user behavior); with a filter set, only a
    # matching address is the avatar account and anyone else is a normal user.
    is_avatar_account = (not targets) or (oauth_email in targets)

    # Persist the refresh token per org for the NATIVE executor + Upcoming
    # (encrypted at rest — store.set_org_oauth). SECURITY: the owning org is
    # derived ONLY from the connecting browser's signed session cookie
    # (auth.current_user), NEVER from the OAuth email or any request field — so a
    # user's token can only ever land on THEIR OWN org, never someone else's. The
    # per-session signed single-use `state` verified above (cookie-bound nonce)
    # is the CSRF guard on this flow, and _oauth_login_required above guarantees a
    # real logged-in owner whenever login is enabled. Falls back to the demo/owner
    # org ONLY on the key-free/no-login demo path (login disabled) — exactly the
    # org the dashboard reads it back from (dashboard.py: caller_org or
    # demo_org_id), so connect + read always agree. Best-effort and independent of
    # native_executor (the flag gates USE, not consent), so enabling native later
    # needs no reconnect. Keyed by the org_id STRING — no ::uuid cast — so
    # u_<hash> session orgs and durable uuid orgs are both safe (never trips the
    # org_id split-brain).
    try:
        _user = auth.current_user(request)
        _org = (_user or {}).get("org_id") or settings.demo_org_id
        _scopes = " ".join(GOOGLE_CALENDAR_SCOPES)
        # Org row — the NATIVE executor (Laura acting on the org's Google account)
        # and the demo/machine Upcoming view. Unchanged.
        await run_in_threadpool(
            store.set_org_oauth,
            _org,
            refresh_token,
            email=oauth_email,
            scopes=_scopes,
        )
        # Per-USER row — the personal-calendar VIEW (/dashboard/upcoming) reads
        # THIS, so a member of a SHARED org (a verified corporate domain maps
        # every colleague onto one org_id) sees only their OWN calendar, never a
        # co-worker's. Keyed on the connecting human from the signed session
        # cookie (auth.current_user) — never the OAuth email or any request field.
        _uid = (_user or {}).get("user_id") or ""
        if _uid:
            await run_in_threadpool(
                store.set_user_oauth,
                _uid,
                refresh_token,
                email=oauth_email,
                scopes=_scopes,
            )
        # A reconnect that ADDED a scope (e.g. calendar.readonly for the
        # all-calendars view) leaves the OLD, still time-valid access token in
        # google_client's cache — old scopes, so calendarList keeps 403ing for
        # up to an hour. Drop the cached token for both principals so THIS
        # instance re-mints with the new grant on the very next read. (Other
        # instances self-heal via the calendarList-403 re-mint; this makes the
        # common single-instance case instant.)
        google_client._drop_cached_token(_org)
        if _uid:
            google_client._drop_cached_token(f"user:{_uid}")
    except Exception as e:  # noqa: BLE001 — enrichment only, never fatal
        print(f"[oauth] native token persist skipped ({type(e).__name__})", flush=True)

    # Recall calendar auto-join is the AVATAR's capability: it registers the
    # avatar's own inbox with Recall so any event that invites its address gets a
    # bot. Only the avatar account (or a single-tenant deployment with no invite
    # filter) may create it — a normal user connecting their own Google just
    # keeps the per-org token stored above and SKIPS this step (their Upcoming
    # comes from their own calendar via google_client.list_calendar_events and
    # they dispatch the avatar manually). This guard is what lets per-user
    # connects be safe without touching the existing auto-join behavior.
    if is_avatar_account:
        try:
            # Side-effecting: registers Laura's calendar with Recall for auto-join.
            # The returned record is no longer surfaced (the callback now redirects
            # to the dashboard), so we don't bind it.
            await run_in_threadpool(
                lambda: recall_client.create_calendar(
                    oauth_client_id=settings.google_calendar_client_id,
                    oauth_client_secret=settings.google_calendar_client_secret,
                    oauth_refresh_token=refresh_token,
                    oauth_email=oauth_email,
                    metadata={
                        "avatar_id": "laura",
                        "invite_filter": ",".join(sorted(targets)),
                    },
                )
            )
        except Exception as e:
            return _clear_calendar_state(JSONResponse(
                {"error": f"Recall calendar creation failed: {e}"}, status_code=400
            ))

    # Land back on the dashboard so the "Google (native)" capability toggle
    # live-refreshes on the next summary load — exactly like the ?brain= Slack
    # return. This is an OAuth redirect target (the browser follows it), never an
    # API a program consumes, so the calendar_id JSON is not needed here; the
    # per-org native refresh token is already persisted above. Consume the
    # single-use CSRF-state cookie on the way out.
    return _clear_calendar_state(
        RedirectResponse("/dashboard?google=connected", status_code=302)
    )


@router.post("/oauth/google/disconnect")
async def google_oauth_disconnect(request: Request) -> JSONResponse:
    """Disconnect Laura's NATIVE Google (Calendar + Gmail) for the caller.

    Fully independent of the Cedric/Slack add-on: this clears the per-org
    native refresh token the executor uses (``store.clear_org_oauth``) AND the
    caller's own per-user calendar token (``store.clear_user_oauth`` — the row
    the personal Upcoming view reads first, so disconnect actually revokes
    what the dashboard uses). A user can drop native Google while keeping
    Slack — or have neither/both. Owner-authed and same-origin, like the brain
    disconnect. The Recall calendar auto-join is a separate capability and is
    intentionally left untouched. Pure SQLite deletes keyed by the id strings —
    no ``::uuid`` cast, so it never trips the u_hash/uuid split-brain."""
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    if not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    cleared = await run_in_threadpool(store.clear_org_oauth, user["org_id"])
    cleared_user = await run_in_threadpool(store.clear_user_oauth, user["user_id"])
    return JSONResponse(
        {
            "ok": True,
            "provider": "google",
            "status": "disconnected",
            "cleared": bool(cleared or cleared_user),
        }
    )


# ── Asana OAuth: the dashboard's one-click "Connect Asana" (docs/ASANA.md) ──
# Same CSRF machinery as the Google flow above: single-use signed state in the
# provider redirect, nonce in an HttpOnly cookie, both must match at the
# callback. Requires the Asana OAuth app env (ASANA_CLIENT_ID/SECRET); without
# it the Connections card falls back to the paste-a-PAT flow.
ASANA_STATE_COOKIE = "laura_asana_oauth_state"
ASANA_STATE_PURPOSE = "asana_state"


def _asana_redirect_uri() -> str:
    return f"{settings.public_base_url.rstrip('/')}/oauth/asana/callback"


@router.get("/oauth/asana/connect")
def asana_oauth_connect(request: Request):
    """Start Asana OAuth: bounce the owner to Asana's consent screen."""
    if not settings.asana_client_id:
        return JSONResponse({"error": "ASANA_CLIENT_ID is not set."}, status_code=400)
    if err := _oauth_login_required(request):
        return err
    nonce, signed_state = auth.issue_oauth_state(ASANA_STATE_PURPOSE)
    params = {
        "client_id": settings.asana_client_id,
        "redirect_uri": _asana_redirect_uri(),
        "response_type": "code",
        "state": signed_state,
        # "default" = the app's configured permissions — the catch-all scope
        # Asana documents for full-access apps.
        "scope": "default",
    }
    resp = RedirectResponse("https://app.asana.com/-/oauth_authorize?" + urlencode(params))
    resp.set_cookie(
        ASANA_STATE_COOKIE,
        nonce,
        max_age=auth.STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.public_base_url.startswith("https"),
        path="/oauth",
    )
    return resp


@router.get("/oauth/asana/callback")
async def asana_oauth_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
):
    """Finish Asana OAuth: verify state, exchange the code, store the grant
    (encrypted per-org, provider="asana-oauth"), land back on Connections.
    Every failure lands with ?asana=error — the dashboard toasts it; no
    half-connected state is ever stored."""
    def _land(result: str):
        resp = RedirectResponse(f"/dashboard?asana={result}", status_code=302)
        resp.delete_cookie(ASANA_STATE_COOKIE, path="/oauth")
        return resp

    if error or not code:
        return _land("error")
    if err := _oauth_login_required(request):
        return err
    cookie_nonce = request.cookies.get(ASANA_STATE_COOKIE, "")
    if not auth.check_oauth_state(state, cookie_nonce, ASANA_STATE_PURPOSE):
        return _land("error")

    exchanged = await run_in_threadpool(
        asana_client.exchange_code, code, _asana_redirect_uri()
    )
    if not exchanged.get("ok") or not exchanged.get("refresh_token"):
        return _land("error")
    # Who/what did we just connect? Best-effort — the grant works regardless.
    info = await run_in_threadpool(
        asana_client.verify_token, exchanged.get("access_token", "")
    )
    user = auth.current_user(request)
    org = user["org_id"] if user else settings.demo_org_id
    try:
        stored = await run_in_threadpool(
            lambda: store.set_org_oauth(
                org, exchanged["refresh_token"], provider="asana-oauth",
                email=str(info.get("email") or exchanged.get("email") or ""),
                scopes=str(info.get("workspace_gid") or ""),
            )
        )
    except RuntimeError:
        return _land("error")  # no encryption key — fails closed
    if not stored:
        return _land("error")
    asana_client._reset_brief_cache()  # new grant → fresh workspace view
    return _land("connected")
