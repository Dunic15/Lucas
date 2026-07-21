# Laura: Product-readiness flow audit (2026-07-13)

Adversarially verified: **28 confirmed** of 39 raw (0 uncertain, 11 refuted). 9 flows × map→find→verify, 58 agents.

## DEMO-BLOCKER (6)

### 1. [join-meeting] Manual /sessions/start never runs the durable cross-instance dedup; a redeploy or cross-path overlap puts a second bot (and meter) in the same call
- **file:** `backend/app/main.py:1191`
- **repro:** start_session dedups ONLY by exact-string `existing.meeting_url == req.meeting_url` against the local SQLite store (main.py:1191-1206) and never calls _meeting_has_active_bot the way the Gmail (main.py:477) and calendar (main.py:2560) paths do; it also never calls _reconcile_duplicate_bots (only the Gmail loop does, main.py:498). The store is ephemeral on App Runner (no persistent disk. CONTEXT.md line 53). Repro: Laura is live in a meeting; the backend redeploys (App Runner rolls the instance) → store is wiped but Recall still has the live bot → the presenter clicks Send Laura again (or the meeting was booked by calendar/Gmail whose stored URL form; hangoutLink vs Zoom `?pwd` vs join.html-normalized; differs from the pasted string) → the local dedup finds nothing → _start_avatar_session mints a second Recall bot at main.py:1108, and no guard on this path catches or evicts it.
- **impact:** Two Lauras in one meeting and two per-minute meters billing simultaneously, triggered by the single most routine ops event (a redeploy); the meter-safety hard constraint is broken on the exact path a customer uses to 'just send her in'. No cleanup ever fires on the manual path.
- **fix:** Call `_meeting_has_active_bot(req.meeting_url)` in start_session before dispatch (return the existing bot's 409), and schedule `_reconcile_duplicate_bots` after a manual start the way the Gmail loop does.

### 2. [dashboard] "Add to Slack" full-page-navigates to a JSON endpoint, so a raw JSON error blob replaces the entire dashboard
- **file:** `frontend/dashboard.html:666`
- **repro:** Key-free demo (or any deploy where CEDRIC_ORGS_URL is unset, or whose ephemeral user row was wiped by a redeploy): open Avatars → the Cedric card → Connect → Add to Slack. The handler does window.location.assign('/dashboard/connections/brain/slack/start?...'). In the key-free demo current_user is None and auth.gate() returns None, so connect_brain_slack_start falls through to `return JSONResponse({"error":"login required"}, 401)` (dashboard.py:486); with CEDRIC_ORGS_URL unset install_url() raises RuntimeError and it returns 503 JSON (dashboard.py:499-500). Because it is a full-page navigation, the browser paints the whole page as `{"error":"login required"}` / `{"error":"brain provisioning is not configured"}`.
- **impact:** A prospect exploring the headline "connect the brain" flow lands on a raw JSON error with the dashboard gone; only the Back button recovers. Reads as broken/half-built and kills confidence mid-demo. The cedric card and this button show for EVERY viewer (roster fail-open + hasBrain=='cedric'), so it is reachable in the key-free demo the product must always support.
- **fix:** Don't navigate the top-level window to a JSON endpoint. Either fetch() the start URL and only window.location.assign the 302 Location on success (handling 401/400/503 as a toast), or make connect_brain_slack_start return an HTML page / RedirectResponse to /dashboard?brain=error on its error paths instead of JSONResponse.

### 3. [dashboard] Cross-tenant meeting leak: every calendar/Gmail auto-joined meeting is stamped with the Demo org, which the dashboard shows to every logged-in customer
- **file:** `backend/app/dashboard.py:235`
- **repro:** visible(row_org) returns True when row_org == settings.demo_org_id for ANY logged-in user. Calendar auto-join stamps org_id=settings.demo_org_id (main.py:2581) and the Gmail watcher calls _start_avatar_session(url, aid) with no org_id, defaulting to settings.demo_org_id (main.py:489 → 1089); and calendar/Gmail auto-join is the primary entry point. So in the multi-email private beta, customer A logs in and their Meetings tab + Recent-meetings table lists customer B's auto-joined meetings, including the AI summary (dashboard.py:100), action items with owner names (dashboard.py:88), and follow-up-email subjects (dashboard.py:104).
- **impact:** A paying beta customer sees another customer's meeting summaries, decisions, action owners and follow-up subjects in their own dashboard. That is a privacy breach of exactly the sensitive meeting content the product promises to keep private; an instant trust-killer that loses the account, even though it is documented as a deferred "isolation lands with Postgres/RLS" seam.
- **fix:** Once auth.enabled(), drop settings.demo_org_id from the visible() allow-set for logged-in users, and stamp auto-join sessions with the org that owns the connected calendar/Gmail account instead of the shared Demo org.

### 4. [brain-connect] Clicking "Add to Slack" replaces the entire dashboard with raw JSON when the brain isn't provisioned (fires in the key-free demo)
- **file:** `frontend/dashboard.html:666`
- **repro:** In the key-free demo (BRAIN_PROVIDER=stub, no login, cedric_orgs_url='' by default at backend/app/config.py:473): open /dashboard → cedric card → Configure → Connect → click "Add to Slack". The handler does window.location.assign('/dashboard/connections/brain/slack/start?...') (a full-page navigation, not fetch). connect_brain_slack_start sees user=None, auth.gate returns None (login disabled), so it falls through to `return JSONResponse({'error':'login required'}, status_code=401)` (backend/app/dashboard.py:486). The browser renders that raw body, so the whole app is replaced by a white page reading {"error":"login required"}. Same for the 400 (dashboard.py:488) and the 503 'brain provisioning is not configured' (dashboard.py:500) branches on any deploy where cedric_orgs_url or the signing key is unset.
- **impact:** The single Connect-the-brain CTA is a hard dead-end in the exact environment used to demo the product: the dashboard vanishes, replaced by a developer JSON error, recoverable only via the browser Back button. A prospect watching the demo sees the app 'crash' the moment they try the flagship integration.
- **fix:** Don't navigate the top frame to an endpoint that can return JSON. Either fetch() slack/start, read a {url} field and window.location.assign(url) only on success (showing a toast on error), or have the endpoint always 302-redirect (to Slack on success, back to /dashboard?brain=error on failure) and never return a JSON body on this route.

### 5. [billing-meter] Double-click / retried "Send Laura" dispatches a SECOND paid bot into the same meeting (two per-minute meters)
- **file:** `backend/app/main.py:1191`
- **repro:** From the dashboard click "Send Laura" twice quickly (or the browser auto-retries POST /sessions/start). Both requests run the in-memory dedup loop `for existing in store.all_sessions()` (main.py:1191-1206) BEFORE either calls store.create() (main.py:1111). There is no lock around start_session, so both concurrent requests see a clash-free store, both call recall_client.create_bot (main.py:1108), and TWO Recall bots join the one meeting. The durable source-of-truth guard `_meeting_has_active_bot` (main.py:370) is wired ONLY into the Gmail watcher (main.py:477) and calendar webhook (main.py:2560); never into /sessions/start.
- **impact:** The customer sees two identical Laura avatars in their call and is billed two Recall per-minute meters for one meeting; exactly the "two bots, two meters" outcome the comment at main.py:1189-1190 warns against. A duplicated avatar mid-demo reads as broken product; the doubled vendor cost is a silent leak on every fat-fingered or retried dispatch.
- **fix:** In /sessions/start call the durable `_meeting_has_active_bot(req.meeting_url)` before dispatch, and/or hold a per-meeting-URL asyncio lock across the guard+create window so concurrent requests serialize.

### 6. [tenancy] Per-org isolation is a no-op for the dominant traffic paths: any logged-in tenant reads every other customer's auto-joined meeting + raw transcript
- **file:** `backend/app/dashboard.py:235`
- **repro:** Deployment has Google login + a beta allowlist with 2+ emails (e.g. an SFF employee and alice@personal.com). SFF's meetings arrive via the product's headline paths; calendar auto-join (main.py:2581), Gmail 'Add people' watcher (main.py:489 -> _start_avatar_session default org main.py:1089), or Cedric's machine bearer (main.py:1188). ALL of which stamp org_id=settings.demo_org_id because there is no authenticated human. visible(row_org) at dashboard.py:235 returns True for demo_org_id for EVERY logged-in user, and /meetings/list applies the same sentinel (main.py:1720-1721) while returning the FULL artifact incl. the raw transcript (persisted at main.py:1453). So alice logs in, GETs /meetings/list, and reads SFF's meeting summary, decisions, action items, and full transcript. This is not hypothetical: the repo's own regression test test_meetings_list_shows_demo_org_rows_to_logged_in_user (test_auth.py:435) locks in that a different-org user sees demo_org rows.
- **impact:** The flagship, just-shipped feature (PR #147 'per-org agents; org_id seam made real', PR #146 'validate RLS isolation', docs/infra/MULTI-TENANCY.md) makes tenant isolation look finished, but for every auto-joined/service meeting; i.e. nearly all real traffic; one customer's confidential meeting transcripts are readable by any other beta user. The moment a prospect asks 'is my meeting data isolated from your other customers?', the honest answer is no. It violates the transcript-PII invariant and kills any enterprise/ops-team sale on the spot.
- **fix:** Stop pooling real traffic into the Demo org: derive org_id for calendar/Gmail/Cedric dispatch from the tenant that owns the calendar/inbox/token (not settings.demo_org_id), and drop demo_org_id from the visible() / meetings_list sentinels for logged-in users (keep '' only for genuine legacy rows). Only true single-tenant demo mode (user is None) should see demo rows.

## ROUGH-EDGE (19)

### 1. [onboarding-login] "Coming soon" waitlist promises an email that is never sent; the verified lead is thrown away
- **file:** `frontend/login.html:122`
- **repro:** Prospect not on DASHBOARD_ALLOWED_EMAILS opens /login, clicks "Continue with Google", completes Google consent. auth.py:274 (`if not email_allowed(claims['email']): _err_redirect('not_allowed')`) discards claims['email'], the just-verified address, and bounces to /login?error=not_allowed, which renders the panel: "We've noted your interest and we'll email you the moment early access opens." A grep across backend/app finds zero waitlist/interest storage; /auth/allowed (auth.py:364-378) returns only booleans and never persists the email either. Nothing was noted; no email will ever be sent. The panel also has no button, so the prospect is stuck.
- **impact:** At the one moment Laura holds a real, Google-verified lead who actively tried to sign up, it lies to them and drops the address. An interested buyer is told they're on a list, waits, and is never contacted; a silently lost sale and a false statement to a prospect.
- **fix:** In the not_allowed branch (auth.py:274) write claims['email'] to a waitlist table (store) before redirecting; OR change the copy to something truthful with an action (e.g. a mailto to sales) so it isn't a dead-end promise.

### 2. [onboarding-login] First-time allowlisted customer can hit Google's "Access blocked" wall with no path forward
- **file:** `backend/app/auth.py:137`
- **repro:** The only sign-in path is Google OAuth against a single OAuth app the code itself documents as being in Google "testing" publishing mode (auth.py:137: "Empty allowlist = allow all (Google's testing-mode test-user list is the gate in that case)"). Owner adds a paying customer's email to DASHBOARD_ALLOWED_EMAILS and tells them to sign in. Customer opens /login, clicks "Continue with Google" (login.html:114 -> /auth/google/start) and Google shows "Access blocked: Laura has not completed the Google verification process" because their account is not in Google's separate Test-users list; which onboarding never adds them to and the login flow never mentions. email_allowed passing is necessary but not sufficient, and there is no in-product fallback or guidance.
- **impact:** A customer who was explicitly granted access is hard-locked out on their very first login with a scary Google security screen and no next step; the worst possible first-use moment. The owner believes allowlisting is enough; it isn't, and nothing surfaces the real gate.
- **fix:** Publish/verify the Google OAuth app so any allowlisted email can complete OAuth (removes the test-user requirement), or make onboarding add the allowlisted email to Google's test-users and show a fallback message on the Access-blocked path. At minimum, correct the auth.py:137/369-371 docstrings so ops knows allowlisting alone doesn't grant Google access.

### 3. [join-meeting] Duplicate-bot / double-meter guards are Google-Meet-only. Zoom & Teams calls get two Lauras and two meters, uncleaned
- **file:** `backend/app/main.py:287`
- **repro:** _meeting_code (main.py:287, regex `meet\.google\.com/([a-z-]+)` at line 267) returns the FULL url for any non-Meet link. The two durable meter-safety guards both key on it: _meeting_has_active_bot (main.py:377-393, test `code in str(mid)` at 390) and _reconcile_duplicate_bots (main.py:332-346). For a Zoom/Teams URL, `code` = the whole `https://zoom.us/j/8412345678?pwd=...` string, while Recall reports the bot's meeting_url as a dict whose `meeting_id` is the opaque numeric/thread id (the code explicitly does `mid = mu.get('meeting_id')` at 345/389). `('https://zoom.us/j/...' in '8412345678')` is always False, so BOTH guards silently no-op. Repro: a Zoom meeting is scheduled via calendar AND the presenter also clicks Send Laura on /join (or a webhook retry / deploy overlap fires) → calendar path's _meeting_has_active_bot at 2560 never matches the live bot → a SECOND bot joins → _reconcile_duplicate_bots at 498 also never matches → nothing evicts it. ledger.py:47-49 already carries the Zoom/Teams regexes that _meeting_code omits, confirming the omission.
- **impact:** Two identical Laura avatars sit in the customer's Zoom/Teams call talking over each other, and two Recall per-minute meters run for one meeting; a visible embarrassment plus a direct violation of the 'meters off / one bot per meeting' invariant. join.html explicitly advertises 'Paste a Google Meet, Zoom, or Teams URL', so a Zoom-first customer hits this on first real use.
- **fix:** Replace _meeting_code with ledger.meeting_key (already platform-aware for Meet/Zoom/Teams, ledger.py:55-67) in both _meeting_has_active_bot and _reconcile_duplicate_bots, and match it against the same platform-key derived from Recall's meeting_id/meeting_url rather than a raw substring test.

### 4. [live-single] leave_call swallows a Recall 5xx, so "Laura, you can leave" can stop the artifact but NOT the per-minute meter
- **file:** `backend/app/recall_client.py:545`
- **repro:** _finalize_session_locked (main.py:1403) does `await run_in_threadpool(recall_client.leave_call, bot_id)`. leave_call (recall_client.py:545-551) issues the meter-stop POST with retry defaulting to False and NO resp.raise_for_status(). _request (recall_client.py:123-149) with retry=False returns the response object even on a 500/502/503 (attempt 0 == attempts-1 → `return resp`). leave_call ignores the status entirely and returns normally. Finalize then treats leave as done, saves the artifact, and calls store.remove(bot_id) (main.py:1502). Because _reconcile_once only polls bots still in store.all_sessions() (main.py:558), the removed bot is never re-checked → the Recall bot stays in the meeting billing per-minute until the humans hang up. Repro: user says a dismissal, Recall's leave_call endpoint returns a transient 503, Laura says goodbye and the recap appears, but the bot keeps recording/billing for the rest of the call.
- **impact:** Directly violates the product's hard meter-stop invariant ("sessions MUST end to stop the per-minute meter"). The customer is silently billed for a bot that appears to have left, with no recovery path; the exact trust/cost failure that kills a paying deployment. Note delete_bot (recall_client.py:427) DOES raise_for_status; the money-critical call is the one that doesn't.
- **fix:** Give leave_call retry=True AND resp.raise_for_status() (mirror delete_bot). In _finalize_session_locked, wrap the leave in try/except and defer store.remove until leave is confirmed (or leave the session in the store so reconcile can re-attempt); never remove a bot whose meter you haven't verified is off.

### 5. [live-single] A mid-stream Cerebras/Groq drop cuts Laura off mid-sentence and 500s the webhook (no try/except on the live answer loop)
- **file:** `backend/app/main.py:3360`
- **repro:** The live answer loop `async for sentence in iterate_in_threadpool(answer_question_stream(...))` (main.py:3360-3398) has no try/except, and there is no app-level exception handler (grep for exception_handler/middleware in main.py returns nothing). llm.stream_complete deliberately re-raises a provider error that happens AFTER the first token to avoid duplicate output (llm.py:237-238: `if yielded or not settings.anthropic_api_key: raise`). So a Cerebras/Groq connection blip once she's already spoken sentence 1 propagates out of recall_webhook → FastAPI 500. The fire-and-forget speak_tasks already created for earlier sentences finish speaking, then she just stops; no fallback line, no Haiku recovery (the breaker only helps on the NEXT turn). If Recall re-delivers the failed transcript.data on the 500, session.add_utterance (main.py:2889) runs again → a duplicate transcript line and a possible re-ack/re-answer.
- **impact:** On the path whose entire selling point is fluent low-latency speech, the customer hears half an answer and then silence with no explanation; reads as the avatar crashing mid-thought. A duplicated transcript line also corrupts the post-meeting artifact/participation stats.
- **fix:** Wrap the streaming loop in try/except; on a mid-stream failure that already spoke, emit one short recovery line (": sorry, I lost my train of thought there") and return 200 so Recall doesn't re-deliver. Consider mid-stream fallback to Haiku for the remaining sentences instead of re-raising.

### 6. [live-single] On thin retrieval Laura answers process questions confidently, uncited, from world knowledge; undercutting the "grounded + cited from your docs" pitch
- **file:** `backend/app/brain.py:567`
- **repro:** answer_question_stream drops all retrieved chunks when the top score is below rag_min_context_score (brain.py:567-568, default 0.28 config.py:235), then ANSWER_STREAM_SYSTEM instructs her to "answer directly and naturally from your own knowledge" and "Never refuse just because it isn't in the documents" (brain.py:169-172). With the KB at 2-3 demo docs (avatars/laura/knowledge = onboarding_sop.md, access_security_sop.md), a real first-use process question the docs don't cover; e.g. "what's our expense-reimbursement approval flow?" or "who signs off on vendor contracts?": retrieves nothing above the floor, so she produces a fluent, confident, UNCITED answer invented from general knowledge instead of "I don't have that in your process docs."
- **impact:** The buyer is specifically evaluating "grounded + cited from OUR process docs." A confident, plausible-but-wrong, uncited answer to a company-specific process question is the worst possible signal; it looks like the product hallucinates policy. (The non-streaming answer_question path DOES honestly strip citations below answer_grounding_floor at brain.py:144-146; the live streaming path has no equivalent "say it's not from your docs" behavior.)
- **fix:** On the streaming path, when chunks were cleared for being below the floor AND the question reads as company/process-specific, bias toward a short "I don't see that in your process docs; want me to answer generally?" rather than an unlabeled world-knowledge answer; or at minimum verbally flag ungrounded process answers as not-from-docs.

### 7. [live-single] first_call_required=True with require_wake_word=False: a first user who never says "Laura" gets total silence; the avatar joined but never speaks
- **file:** `backend/app/config.py:313`
- **repro:** first_call_required defaults True (config.py:313), so _in_opening_grace returns True forever until session.addressed_once is set (main.py:2324-2330), and the final-transcript gate returns "opening grace" (silent) for every unaddressed line (main.py:2966-2967). Meanwhile require_wake_word defaults False (config.py:181) and the positioning is "she answers any groundable question without needing her name." A first-time user who follows that positioning, joins, asks questions, never once says "Laura", gets zero responses for the entire meeting even though her face is live on the bot's camera.
- **impact:** An avatar that visibly joined the call but never answers reads as broken, not as "waiting to be named." The two shipped defaults contradict each other ("no wake word needed" vs "mute until named once"), so the failure is most likely exactly when a prospect tries the frictionless flow the marketing promised.
- **fix:** Either default first_call_required=False (fall back to the time-boxed opening_grace_seconds so she activates after the room settles) or have her speak one unprompted "Hi, I'm Laura; say my name whenever you need me" line shortly after join so the user knows the activation gesture.

### 8. [live-multi] roster() hardcodes 'laura', dropping a human named Laura in any non-Laura avatar meeting
- **file:** `backend/app/store.py:216`
- **repro:** roster() builds skip = {avatar_name.lower(), 'laura', ''} regardless of the loaded avatar. Cedric/SFF/Duccio are real avatars under avatars/. In a Cedric meeting with the owner + a human participant named Laura, 'laura' is stripped from the roster, so roster(avatar.name) returns just [owner] (count 1). Consequences: (a) the multi-human hand-raise gate len(roster) >= hand_raise_min_humans (main.py:3353) fails, so hand-raise etiquette never engages in a room that IS multi-person, and adaptive-deference n_humans (main.py:3244) is mis-sized; (b) 'how many are we?' answers wrong and she never greets/nudges the human Laura by name; (c) roster drops to <=1, arming the unaddressed 1:1 early-leave branch (main.py:3048); owner saying 'ok, Laura you can leave' (dismissing the human) passes plausible_leave_followup on the 'ok' lead and detect_leave_command on 'you can leave', so Cedric leaves the paid call mid-meeting.
- **impact:** A named attendee is invisible to the avatar in any non-Laura deployment: wrong headcount, no personalized address, broken hand-raise etiquette, and a possible early bot-exit that truncates the meeting and its artifact. Undermines the 'adding an avatar = adding a folder, no backend code' story the moment a customer's team has a 'Laura' on staff.
- **fix:** Drop the persona literal: skip = {avatar_name.strip().lower(), ''}. The avatar name already flows in from the loaded avatar via the avatar_name argument.

### 9. [live-multi] Confident interjection can talk over a human when Recall partials lag on the live path
- **file:** `backend/app/main.py:3452`
- **repro:** In hand_mode the whole contribution is generated (deference sleep at main.py:3250, then the full answer_question_stream collected, several seconds) BEFORE the talk-over decision. interjection_floor_open (decision.py:527-550, called at main.py:3452-3457) judges 'is someone talking right now' ENTIRELY from since_human_partial = now - session.last_human_partial_at, and last_human_partial_at is written ONLY on the transcript.partial_data path (main.py:2726). If a human starts a NEW turn during generation and Recall's partial for it is delayed or dropped, since_human_partial stays large, the floor is judged open (turn_completeness is computed off the STALE triggering line, main.py:3453), and should_interject speaks a full grounded line over the human via _speak_with_audio (main.py:3468).
- **impact:** On the 'latency is the product' live path, her stepping on a speaker is the single most visible failure of the multi-person etiquette the hand-raise exists to provide; the opposite of the 'she waited politely and jumped in with the right fact' wow-moment. One talk-over in a live prospect meeting sinks the 'natural teammate' story.
- **fix:** Treat transcript growth since turn start (a new final landed) or elapsed generation time as an additional floor-busy signal, and re-evaluate last_human_partial_at at interjection time rather than trusting only the triggering line's completeness; when uncertain, fall back to the silent raised hand.

### 10. [dashboard] Meetings tab and its Excel filters silently operate over only the newest 60 rows while headline counts include all meetings
- **file:** `backend/app/dashboard.py:402`
- **repro:** The wire truncates to meetings[:60] (dashboard.py:402), but stats.meetings_30d (dashboard.py:349) and per-avatar meetings_total (dashboard.py:319) are computed over the full uncapped visible list, and the client filters run over the ≤60 delivered rows with a 999 limit and no "showing N of M" hint (dashboard.html:743). With >60 meetings the Overview tile shows e.g. "Meetings 30d: 80" while the Meetings table lists at most 60; filtering "Last 7 days" then shows a subset that doesn't reconcile with the headline, and the 61st+ oldest meetings are unreachable.
- **impact:** A customer whose volume passes 60 sees numbers that don't add up and older meetings that vanish with no explanation; it looks like lost data and undermines trust in the dashboard as a system of record.
- **fix:** Surface a "showing newest 60 of N" indicator and/or paginate the Meetings tab; at minimum compute the tile/roster counts from the same 60 the UI can actually show, or raise the cap.

### 11. [post-meeting] Any transient post-meeting LLM error 500s the end-meeting call and loses the entire deliverable + delivery
- **file:** `backend/app/main.py:1427`
- **repro:** With a real post brain (prod is BRAIN_PROVIDER_POST=anthropic/cerebras), a paying customer ends a real meeting via POST /sessions/{id}/end. The post model returns a transient 429/529-overloaded/timeout. brain.post_meeting (brain.py:1179) calls llm.complete with an EXPLICIT provider=post_provider(), so the 'never go dark' Haiku fallback at llm.py:147 (gated on `provider is None`) never runs and the error re-raises. _finalize_session_locked has NO try/except around post_meeting at main.py:1427 (contrast ledger at 1472 and autopilot at 1486, both wrapped), so it propagates through _finalize_session and end_session (main.py:1529, no @app.exception_handler) → HTTP 500. Because it raises before line 1468, store.save_artifact, the session.ended callback (1479), store.remove (1502) and gpu_runtime/runpod_runtime.on_session_ended (1505-1506) are all skipped.
- **impact:** At the exact moment the customer ends their first real paid meeting they get a 500 and their recap/summary/action items/follow-up email + Slack/callback delivery all vanish. The Recall meter IS stopped (leave_call at main.py:1403 runs first), but the photoreal GPU meter is not signaled off until a later reconcile pass. The reconcile loop self-heals minutes later, but the immediate deliverable and delivery are gone and the visible result is a hard error; the product's headline output failing on turn one loses the sale. The degraded-JSON rescue at brain.py:1194 only catches malformed JSON, never an exception.
- **fix:** Wrap the post_meeting call in try/except and fall back to _stub_post_meeting(..., degraded=True) so a model hiccup degrades to a plain deterministic recap (as the degraded-JSON path already does) instead of a 500 + total loss; or let the explicit-provider post path fall back to Haiku the way the provider=None live path does.

### 12. [post-meeting] 'Retry delivery' (POST /sessions/{id}/redeliver) blocks the HTTP request ~150s and 504s when the orchestrator is down
- **file:** `backend/app/main.py:1685`
- **repro:** Owner/machine hits POST /sessions/{id}/redeliver to re-fire a failed session.ended callback while the orchestrator endpoint is unreachable. main.py:1685 AWAITs run_in_threadpool(callback.send_ended,...), and send_ended retries 4 times with blocking time.sleep across ENDED_BACKOFF 5s+25s+120s (callback.py:223) plus up to callback_timeout_seconds=10 per attempt. The request hangs ~150-190s and will 504 at the App Runner/browser proxy before it returns. deliver_ended (integration.py:179) fires the identical call as a non-blocking asyncio.create_task; the two paths are inconsistent.
- **impact:** The one tool meant to RECOVER a failed delivery gives multi-minute dead air and then a gateway error, exactly when the orchestrator being down is the reason you clicked retry. The caller can't tell whether it eventually succeeded; erodes trust at the worst moment.
- **fix:** Make redeliver fire-and-forget (asyncio.create_task, return 202 'retrying') like deliver_ended, or do a single bounded non-retrying attempt and report its result synchronously.

### 13. [post-meeting] Manual /deliver stamps the Slack follow-up with the wrong avatar name ('Laura') for non-Laura meetings
- **file:** `backend/app/main.py:1592`
- **repro:** A Cedric (or any non-default) avatar meeting finalizes; finalize already ran store.remove(bot_id). Owner then calls POST /sessions/{id}/deliver. main.py:1592 does `avatars.load(store.get(bot_id).avatar_id) if store.get(bot_id) else None`, store.get(bot_id) is ALWAYS None post-finalize, so name falls back to avatars.load(settings.default_avatar_id).name = 'Laura', even though artifact['avatar_id']=='cedric' is present. actions.artifact_to_slack_text(name, artifact) then renders the header '*Laura; meeting follow-up*'.
- **impact:** A customer running a Cedric/other-branded avatar sees the wrong agent's name on the manually delivered recap. The finalize/autopilot delivery path (main.py:1487) uses the correct session.avatar_id, so the two delivery paths disagree; looks broken and off-brand.
- **fix:** Resolve the name from artifact.get('avatar_id') (always present after finalize) before falling back to settings.default_avatar_id.

### 14. [post-meeting] Every finished meeting's artifact, /meetings archive, and redelivery are wiped on each App Runner redeploy
- **file:** `backend/app/store.py:28`
- **repro:** App Runner has no persistent disk and auto-deploys on every main push. _default_store_path (store.py:28-32) checks for /var/data, which doesn't exist, so it falls back to the container-local backend/data/store.sqlite3. On redeploy the new container starts with an empty DB → _load_from_db() (store.py:527) loads nothing → GET /meetings/list, GET /sessions/{id}/artifact, POST /sessions/{id}/deliver and POST /sessions/{id}/redeliver all 404 for every prior meeting.
- **impact:** A paying customer who returns the next day (or after any deploy) to read or re-send their meeting recap finds it gone, with no recovery path. The /meetings archive and redeliver features look durable but silently lose all history on every deploy. CONTEXT flags SQLite as ephemeral, but the product UI presents these recaps as permanent.
- **fix:** Point LAURA_STORE_PATH (already honored at store.py:35) at a durable backing store, managed Postgres/RDS or an S3/EFS-backed volume, for the App Runner deployment.

### 15. [billing-meter] leave_call swallows Recall 5xx: the meter never stops and the session is deleted so it can never be retried (permanent bill leak)
- **file:** `backend/app/recall_client.py:545`
- **repro:** End a live meeting via POST /sessions/{bot_id}/end or voice dismissal when Recall returns a transient 500/502/503/504 to leave_call. leave_call (recall_client.py:545-551) passes no retry flag (default retry=False → 1 attempt) and calls no raise_for_status(). _request returns the 5xx response on its last/only attempt (recall_client.py:141-142, statuses {429,500,502,503,504}) and leave_call discards it as success. _finalize_session_locked then runs store.remove(bot_id) (main.py:1502), so the still-live bot leaves store.all_sessions() and the 60s reconcile backstop (main.py:552) can never re-poll it. Contrast delete_bot (recall_client.py:418-427: retry=True + raise_for_status).
- **impact:** The bot stays in the customer's call and the Recall per-minute meter keeps billing until the humans hang up, with zero automatic recovery (only a manual Recall API sweep). Directly violates the hard invariant "sessions MUST end to stop the per-minute meter". The user pressed End, saw success, and is still being charged.
- **fix:** Make leave_call use retry=True and raise_for_status() like delete_bot; in _finalize_session_locked only store.remove() AFTER leave_call is confirmed, leaving the session in the store (so reconcile retries) when the meter-stop failed.

### 16. [billing-meter] A deploy/restart during a live call orphans the meter AND /health falsely reports active_sessions:0
- **file:** `backend/app/store.py:29`
- **repro:** A customer is in a live meeting (bot billing) when main is pushed. App Runner auto-deploys on every push and replaces the instance. Sessions persist only to an ephemeral SQLite file (store.py:29-32: /var/data is absent on App Runner so it falls back to the baked-empty data/store.sqlite3), so the new instance boots with zero sessions. _lifespan (main.py:100-148) does NOT sweep Recall for live bots at boot, and _reconcile_once iterates store.all_sessions() (main.py:552), now empty, so the live bot is never polled or finalized. GET /health returns active_sessions = len(store.all_sessions()) = 0 (main.py:644).
- **impact:** The avatar in the customer's call goes dead (no answers) yet the Recall meter keeps billing until the humans leave; a broken avatar that still costs money. The operator's orphan runbook (.claude/commands/check-sessions.md:8-9) says to trust /health active_sessions==0 as the all-clear, so the leak is invisible. Violates the meter-off hard invariant.
- **fix:** On startup in _lifespan, sweep Recall's bot list for non-terminal bots and re-adopt them into the store (so reconcile finalizes them) or leave_call them; and/or make /health cross-check Recall rather than only the ephemeral local store.

### 17. [billing-meter] Usage & billing panel undercounts (or zeroes) real cost: minutes come from transcript-timestamp span, not bot uptime
- **file:** `backend/app/main.py:1463`
- **repro:** Run a real ~30-min meeting where talking is sparse (long silent stretch, or the bot sits in a waiting room). duration_seconds is computed as transcript[-1].ts - transcript[0].ts (main.py:1463-1466), the span between first and last captured utterance, NOT bot uptime, and is omitted entirely when fewer than 2 utterances are captured. The dashboard defaults the missing value to 0 (dashboard.py:98) and sums these into total_minutes / minutes_30d / est_cost_30d (dashboard.py:370-380). A meeting with <2 transcribed lines shows 0 minutes and $0.00 despite a real billed bot.
- **impact:** The buyer-facing Usage & billing cost figure can be materially lower than the real Recall bill (or literally $0 for a quiet meeting that actually ran), so the estimate the customer uses to reason about spend is wrong on the low side and misrepresents what Laura costs to run.
- **fix:** Record real bot uptime from Recall status_change timestamps (join → leave/terminal) and use that for duration_seconds; never fall back to 0 for a meeting whose bot demonstrably ran.

### 18. [tenancy] Usage & billing panel and headline stats sum other tenants' meetings into one customer's numbers
- **file:** `backend/app/dashboard.py:370`
- **repro:** On /dashboard/summary the `meetings` list (dashboard.py:239) is filtered only by visible(), which admits every demo_org row. billing.total_minutes (dashboard.py:370), billing.est_cost_30d (dashboard.py:380), and the stats block (hours_30d dashboard.py:350, meetings_30d, actions_30d, hours_saved_30d) are all computed over that list. Because every auto-joined/service meeting is stamped demo_org_id, a logged-in customer's 'Usage & billing' and ROI tiles include minutes, cost, and action counts generated by other tenants (and by the founder's own demo/test runs).
- **impact:** A customer opening their own dashboard sees usage hours, an estimated cost, and 'hours saved' that are neither private nor theirs; inflated by unrelated activity. It reads as either a billing bug (why am I being charged for meetings I never had?) or a privacy tell (whose meetings are these?), undermining trust in exactly the panel meant to justify the price.
- **fix:** Scope the billing/stats aggregation to store.list_artifacts(user['org_id']) (the org-sealed query already exists) rather than the demo-widened visible() list, so a logged-in tenant's numbers only reflect their own org.

### 19. [tenancy] A logged-in user can force-end another tenant's in-progress auto-joined meeting (kills the Recall bot + triggers premature delivery)
- **file:** `backend/app/main.py:1525`
- **repro:** SFF has a live meeting auto-joined by calendar/Gmail, so its session carries org_id=demo_org_id. A different beta user (alice) logs in; /dashboard/summary's live[] block emits that session's bot_id to her because visible(demo_org_id) is True (dashboard.py:262-270). She POSTs /sessions/{bot_id}/end; the guard at main.py:1525-1528 only blocks a DIFFERENT real org, and demo_org_id is in the allow set, so the call proceeds to _finalize_session; ending SFF's live meeting, leaving the Recall bot, and firing the post-meeting artifact/delivery. The repo's test_logged_in_user_can_end_demo_org_session (test_auth.py:419) confirms this is the shipped behavior.
- **impact:** One customer (or any beta user) can terminate another customer's live meeting mid-call and trigger an early, possibly wrong, follow-up delivery; a cross-tenant denial-of-service on the single most visible moment of the product (the avatar sitting in the call). Combined with the transcript leak it makes the multi-tenant control plane feel unsafe to hand to a real team.
- **fix:** Don't expose demo_org bot_ids to arbitrary logged-in users and don't let them end demo_org sessions; scope the manual meter-kill switch to the caller's own org (and provide an admin-only override for genuine shared/service sessions).

## POLISH (3)

### 1. [dashboard] The ROI / "delivered" outcome story the backend computes is never rendered; the dashboard only shows raw counts
- **file:** `backend/app/dashboard.py:359`
- **repro:** Backend produces per-meeting `delivered` chips (dashboard.py:107 via _delivered, 61-80) and stats.actions_executed_30d / followups_automated_30d / hours_saved_30d / roi_minutes_per_action (dashboard.py:359-364). grep of frontend/dashboard.html for delivered, hours_saved, actions_executed, followups_automated, roi_minutes returns ZERO matches; the tiles (dashboard.html:602-604) render only meetings_30d/hours_30d/actions_30d/followups_30d/avg_readiness, and rows never render `delivered`.
- **impact:** The buyer-facing "what Laura DID / hours saved" framing, the value proposition the code went out of its way to build, is invisible. The dashboard reads as a passive note-taker instead of an outcome engine, weakening the core sales narrative for no functional reason.
- **fix:** Render stats.hours_saved_30d as a tile and the per-meeting `delivered` chips in the meeting row/detail; both are already on the wire and derived only from distilled counts (no PII risk).

### 2. [dashboard] "Actions captured" headline metric undercounts because each meeting's actions are capped at 12 before summing
- **file:** `backend/app/dashboard.py:88`
- **repro:** _meeting_row truncates actions to (art.get('actions') or [])[:12] (dashboard.py:88); stats.actions_30d then sums len(m['actions']) over those capped lists (dashboard.py:337), and hours_saved_30d is derived from that count (dashboard.py:361). A meeting that produced >12 action items shows only 12 in the expand-detail AND undercounts the "Actions captured" tile and the ROI hours-saved estimate.
- **impact:** On a busy meeting the customer's own value metric reads lower than what Laura actually captured; the dashboard undersells the product's output, and the missing action items are silently dropped from the detail view.
- **fix:** Keep the [:12] cap for the detail projection if desired, but compute stats.actions_30d from the artifact's true action count (or lift the cap for the count), so the headline reflects everything captured.

### 3. [post-meeting] /demo/post_meeting (and /demo/sample) return a raw 500 for an unknown avatar_id
- **file:** `backend/app/main.py:768`
- **repro:** POST /demo/post_meeting {"avatar_id":"laura","transcript":"..."} (a typo, or any API caller not using the dropdown). main.py:768 calls avatars.load(req.avatar_id) with no guard; avatars.load raises FileNotFoundError (avatars.py:112-116) → unhandled HTTP 500. The demo page surfaces it as a bare 'HTTP 500' (demo.html:131, since FastAPI's error body has no `error` key). Same unguarded load on /demo/sample at main.py:779.
- **impact:** An evaluator poking the API with a mistyped avatar id gets a server-error instead of a clean 'unknown avatar': reads as a fragile backend. Low likelihood via the demo dropdown.
- **fix:** Catch FileNotFoundError around avatars.load and return 404 {'error':'unknown avatar_id'} with the available list.


## Synthesis

Read CONTEXT.md and cross-referenced all 28 confirmed defects against the invariants. Deduped to 20 clustered items (three demo-blocker clusters absorb the near-duplicates). Here is the prioritized fix list.

---

# Laura: Prioritized Fix List (path to a sellable product)

## DEMO-BLOCKERS (fix before any customer touches it)

**1. Cross-tenant transcript & meeting leak; every logged-in tenant reads every other customer's auto-joined meetings**
- Flows: tenancy, dashboard (folds in the 4 tenancy/dashboard leak findings)
- `backend/app/dashboard.py:235` (visible sentinel) + `backend/app/main.py:1720` (/meetings/list, returns full transcript) + write side `main.py:2581`, `main.py:1089`, `main.py:1188` (auto-join/Gmail/Cedric all stamp `demo_org_id`)
- Fix: once `auth.enabled()`, drop `settings.demo_org_id` from the `visible()` / meetings-list allow-set for logged-in users, and stamp auto-join/Gmail/Cedric sessions with the org that owns the calendar/inbox/token instead of the shared Demo org. Same change also seals the /end guard (`main.py:1525`) and the billing/stats aggregation (`dashboard.py:370`).
- Effort: **M**

**2. "Add to Slack" full-page-navigates to a JSON endpoint; a raw error blob replaces the whole dashboard (fires in the key-free demo)**
- Flows: dashboard, brain-connect (the two Add-to-Slack findings are one bug)
- `frontend/dashboard.html:666` → JSON at `backend/app/dashboard.py:486`/`500`
- Fix: `fetch()` the start URL and only `window.location.assign` the 302 Location on success (toast on 401/400/503); or make `connect_brain_slack_start` always 302 (to Slack on success, to `/dashboard?brain=error` on failure) and never return a JSON body on that route.
- Effort: **S**

**3. Duplicate-bot / double-meter: manual `/sessions/start` never runs the durable guard, and the guard it skips is Meet-only anyway**
- Flows: join-meeting, billing-meter (folds double-click, redeploy/cross-path, and Zoom/Teams findings)
- `backend/app/main.py:1191` (manual start dedups only on exact-string local store) + `main.py:287` (`_meeting_code` is Meet-only, so the durable guard no-ops on Zoom/Teams)
- Fix: call `_meeting_has_active_bot(req.meeting_url)` in `start_session` before dispatch, hold a per-meeting-URL asyncio lock across the check→`create_bot` window, and swap `_meeting_code` for the platform-aware `ledger.meeting_key` in both guards so Zoom/Teams match too.
- Effort: **M**

## ROUGH EDGES (a paying customer will hit these in normal use)

**4. `leave_call` swallows a Recall 5xx. "you can leave" stops the artifact but not the per-minute meter**
- Flow: live-single, billing-meter · `backend/app/recall_client.py:545`
- Fix: give `leave_call` `retry=True` + `resp.raise_for_status()` (mirror `delete_bot`); in `_finalize_session_locked` defer `store.remove` until leave is confirmed so reconcile can retry.. **S**

**5. Any transient post-meeting LLM error 500s the end-meeting call and loses the whole deliverable + delivery**
- Flow: post-meeting · `backend/app/main.py:1427`
- Fix: wrap the `post_meeting` call in try/except → fall back to `_stub_post_meeting(degraded=True)` (like the degraded-JSON path) instead of propagating a 500.. **S**

**6. `roster()` hardcodes `'laura'`: a human named Laura vanishes in any non-Laura avatar meeting (wrong headcount, broken hand-raise, can early-exit a paid call)**
- Flow: live-multi · `backend/app/store.py:216`
- Fix: `skip = {avatar_name.strip().lower(), ""}`: drop the persona literal.. **S**

**7. Manual `/deliver` stamps the Slack follow-up with "Laura" for non-Laura meetings (and it's the default delivery path when autopilot is off)**
- Flow: post-meeting · `backend/app/main.py:1592`
- Fix: resolve the name from `artifact.get("avatar_id")` (always present post-finalize) before falling back to `default_avatar_id`.. **S**

**8. `first_call_required=True` + `require_wake_word=False`: a first user who never says "Laura" gets total silence for the whole meeting**
- Flow: live-single · `backend/app/config.py:313`
- Fix: default `first_call_required=False` (fall back to time-boxed opening grace), or have her speak one unprompted "Hi, I'm Laura; say my name when you need me" line after join.. **S**

**9. On thin retrieval Laura answers process questions confidently and uncited from world knowledge; undercuts the "grounded + cited from your docs" pitch**
- Flow: live-single · `backend/app/brain.py:567`
- Fix: on the streaming path, when chunks were cleared for being below the floor AND the question reads company/process-specific, bias to "I don't see that in your process docs; want me to answer generally?" instead of an unlabeled answer.. **M**

**10. A mid-stream Cerebras/Groq drop cuts Laura off mid-sentence and 500s the webhook (no try/except on the live answer loop)**
- Flow: live-single · `backend/app/main.py:3360`
- Fix: wrap the streaming loop; on a mid-stream failure that already spoke, emit one short recovery line and return 200 (so Recall doesn't re-deliver and duplicate the transcript). Optionally mid-stream fall back to Haiku.. **S**

**11. Usage & billing minutes come from the transcript-timestamp span, not bot uptime; a quiet/waiting-room meeting shows 0 min / $0.00 despite a real bill**
- Flow: billing-meter · `backend/app/main.py:1463`
- Fix: compute `duration_seconds` from Recall status-change timestamps (join → leave/terminal); never fall back to 0 for a bot that demonstrably ran.. **M**

**12. Confident hand-raise interjection can talk over a human when Recall partials lag at turn onset**
- Flow: live-multi · `backend/app/main.py:3452`
- Fix: add transcript-growth-since-turn-start / elapsed-generation-time as a floor-busy signal, re-evaluate `last_human_partial_at` at interjection time, and fall back to the silent raised hand when uncertain.. **M**

**13. Ephemeral SQLite store; every finished meeting's artifact / `/meetings` archive / redelivery is wiped on each App Runner redeploy, and a deploy during a live call orphans the bot while `/health` falsely reports `active_sessions:0`**
- Flows: post-meeting, billing-meter (folds the two store-durability findings)
- `backend/app/store.py:28` (ephemeral path) + `backend/app/main.py:100` (no boot-time Recall sweep) + `main.py:644` (/health reads local store only)
- Fix: point `LAURA_STORE_PATH` at a durable backing store (RDS/Postgres or S3/EFS-backed volume, or activate the already-built Litestream restore-on-boot); on startup sweep Recall for non-terminal bots and re-adopt them; make `/health` cross-check Recall.. **L**

**14. Meetings tab + Excel filters silently operate over only the newest 60 rows while headline counts include all meetings**
- Flow: dashboard · `backend/app/dashboard.py:402`
- Fix: surface a "showing newest 60 of N" indicator and/or paginate; or compute the tile counts from the same 60 the UI can show.. **S**

**15. `/redeliver` blocks the HTTP request ~150s and 504s when the orchestrator is down; the recovery tool dies exactly when you need it**
- Flow: post-meeting · `backend/app/main.py:1685`
- Fix: make redeliver fire-and-forget (`asyncio.create_task`, return 202) like `deliver_ended`, or do a single bounded non-retrying attempt.. **S**

**16. "Coming soon" waitlist promises an email that is never sent; the Google-verified lead is discarded, panel is a dead-end**
- Flow: onboarding-login · `frontend/login.html:122` / `backend/app/auth.py:274`
- Fix: persist `claims["email"]` to a waitlist store before redirecting, OR change the copy to a truthful action (mailto to sales) so it isn't a false promise.. **S**

**17. First-time allowlisted customer can hit Google's "Access blocked" wall (app in Testing mode) with no in-product path forward**
- Flow: onboarding-login · `backend/app/auth.py:137`
- Fix: publish/verify the Google OAuth app (scopes are non-sensitive: openid/email/profile), or have onboarding add the allowlisted email as a Google test-user; at minimum correct the `auth.py:137`/`369` docstrings so ops knows allowlisting alone isn't enough.. **S**

## POLISH (narrative / hardening, no visible break)

**18. The ROI / "hours saved" / per-meeting `delivered` outcome story the backend computes is never rendered; dashboard reads as a passive note-taker**
- Flow: dashboard · `backend/app/dashboard.py:359` (fields on the wire, zero consumers in `dashboard.html`)
- Fix: render `stats.hours_saved_30d` as a tile and the per-meeting `delivered` chips (both already distilled, no PII risk).. **S**

**19. "Actions captured" headline metric + ROI hours undercount because each meeting's actions are capped at 12 before summing**
- Flow: dashboard · `backend/app/dashboard.py:88`
- Fix: keep the `[:12]` cap for the detail projection if desired, but compute `actions_30d` from the artifact's true action count.. **S**

**20. `/demo/post_meeting` and `/demo/sample` return a raw 500 for an unknown `avatar_id`**
- Flow: post-meeting (demo API) · `backend/app/main.py:768`
- Fix: catch `FileNotFoundError` around `avatars.load` → 404 `{"error":"unknown avatar_id"}` with the available list.. **S**

---

## Executive read

**Fix first: the cross-tenant leak (#1).** For a product whose entire promise is confidential meeting handling, having every logged-in beta tenant read every other customer's summaries, decisions, action owners and full transcripts; because auto-join pools all real traffic into the shared Demo org and `visible()` shows Demo-org rows to everyone; is a hard-constraint (PII) violation that kills any enterprise/ops sale on the first "is my data isolated?" question. It's an M-effort change and the just-shipped "per-org isolation" feature is currently a no-op without it.

**Demo-ready today: No.** Two things gate the *demo itself*: (a) the "Add to Slack" CTA (#2) replaces the whole dashboard with a raw JSON error in the exact key-free environment the product is demoed in, and (b) a double-click / retry on "Send Laura" (#3) puts a second avatar and a second per-minute meter into the same call. Both are cheap (S/M) and visible on-camera. A tightly-scripted single-operator demo that avoids those two clicks will run; but that's not the same as ready. Land #1–#3 plus the two money-safety S-fixes (#4 leave_call, #5 post-meeting 500) and you have a demo you can hand to a prospect without a spotter; the remaining rough edges are the gap between "demos well" and "a team can safely run it unattended."