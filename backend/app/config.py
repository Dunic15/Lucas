"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = two levels up from this file (backend/app/config.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Reasoning (the brain) — pick a provider:
    #   anthropic (best quality, needs ANTHROPIC_API_KEY)  ← default
    #   cerebras  (FASTEST live, ~0.17s; OpenAI-compatible, needs CEREBRAS_API_KEY)
    #   groq      (fast + cheap; OpenAI-compatible, needs GROQ_API_KEY)
    #   ollama    (free, local, needs Ollama running)
    #   stub      (free, offline, no model — deterministic, for a zero-key demo)
    # NOTE: prod today runs BRAIN_PROVIDER=cerebras for the live spoken path and
    # BRAIN_PROVIDER_POST=anthropic (Sonnet 5) for the post-meeting artifact.
    brain_provider: str = "anthropic"
    anthropic_api_key: str = ""
    brain_model: str = "claude-sonnet-5"
    # Post-meeting provider override: the live path keeps BRAIN_PROVIDER (speed),
    # while the artifact/summary can use a different provider for quality —
    # e.g. BRAIN_PROVIDER=groq + BRAIN_PROVIDER_POST=anthropic +
    # BRAIN_MODEL=claude-sonnet-5. Empty = same provider everywhere.
    brain_provider_post: str = ""
    # Latency-critical live-answer path uses a faster/cheaper model; the quality
    # model above is reserved for the non-realtime post-meeting summary.
    brain_model_fast: str = "claude-haiku-4-5"
    # Tiered live routing: clearly-complex questions (analyze/compare/plan/…) go
    # to this Claude model directly instead of the fast Groq model — more reliable
    # and capable, and it skips Groq's rate limits. Needs ANTHROPIC_API_KEY; empty
    # provider check falls back to the fast model. Haiku (not Sonnet) so the LIVE
    # spoken path stays low-latency; Sonnet is reserved for the post-meeting brain.
    brain_model_complex: str = "claude-haiku-4-5"

    # Ollama (only used when BRAIN_PROVIDER=ollama)
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    # Groq (fast, cheap open models via an OpenAI-compatible API).
    # BRAIN_PROVIDER=groq; set BRAIN_MODEL / BRAIN_MODEL_FAST to a Groq model id
    # (e.g. llama-3.3-70b-versatile). ~0.2-0.4s first token, no latency spikes.
    groq_api_key: str = ""
    groq_base: str = "https://api.groq.com/openai/v1"

    # Cerebras (fastest live inference, ~0.17s first token; same OpenAI-compatible
    # wire format as Groq, different endpoint + models). BRAIN_PROVIDER=cerebras;
    # set BRAIN_MODEL_FAST to a Cerebras model id (e.g. gemma-4-31b, gpt-oss-120b).
    # This is what prod uses on the live spoken path — it's the latency play.
    # (History: this used to be smuggled in via GROQ_BASE=https://api.cerebras.ai/v1
    # with the Cerebras key stored under GROQ_API_KEY — now a first-class provider.)
    cerebras_api_key: str = ""
    cerebras_base: str = "https://api.cerebras.ai/v1"

    # Vertex AI (Google Gemini) — brain provider that bills to GCP, so it can run
    # on Google Cloud credits (the GCP Free Trial does NOT cover the AI Studio
    # Gemini API, but DOES cover Vertex AI). Opt-in: BRAIN_PROVIDER=vertex. Auth
    # via a service account with roles/aiplatform.user — set
    # GOOGLE_APPLICATION_CREDENTIALS to its JSON, or rely on ADC. The live spoken
    # path stays on cerebras/anthropic; this is for the text brain (and free-tier
    # experimentation). gemini-3.5-flash requires VERTEX_LOCATION=global.
    vertex_project: str = ""              # GCP project id, e.g. "868562221752"
    vertex_location: str = "global"       # "global" serves 3.x; a region also works for 2.5
    vertex_model: str = "gemini-2.5-flash"  # brain model (set gemini-3.5-flash on global)
    # Realtime voice (Gemini Live) — used ONLY by the standalone spike, never the
    # live meeting path. gemini-live-2.5-flash on the global websocket host.
    vertex_live_model: str = "gemini-live-2.5-flash"
    # Vertex auth for environments without gcloud/ADC (App Runner): the FULL
    # service-account JSON, passed via env/SSM (GOOGLE_VERTEX_SA_JSON). When set,
    # llm._vertex_token() mints tokens from it instead of ADC. Never in git.
    google_vertex_sa_json: str = ""

    # Gemini ears (issue: realtime turn-taking) — stream the meeting's mixed
    # audio (Recall realtime websocket) into Gemini Live for native STT +
    # natural end-of-turn detection. Three modes:
    #   off    (default) — exactly today's behavior; no audio endpoint, no ears.
    #   shadow — ears run alongside real meetings: transcribe + detect turns,
    #            record METRICS ONLY (counts/timing — transcripts are PII and
    #            are never logged). Zero effect on the live decision path.
    #   on     — ears are authoritative: Gemini turns feed the SAME webhook
    #            pipeline (synthesized transcript.data, speaker merged from
    #            Recall finals); raw Recall finals are suppressed while the
    #            ears session is healthy, and processing falls back to them
    #            automatically if it dies. Flip only after shadow validation.
    #   reply  — "on" + tutto-Gemini: the Live model also drafts the SPOKEN
    #            reply (rides the synthesized payload as laura_ears_reply);
    #            gates still decide whether to speak, ElevenLabs still speaks,
    #            but the brain (RAG) is bypassed — instant feel, no grounding.
    gemini_ears_mode: str = "off"
    # Base URL the ears session uses to POST synthesized finals back into the
    # app (on-mode only). Empty = http://127.0.0.1:$PORT (same container).
    self_base_url: str = ""
    # Gemini ears RELAY: App Runner can't accept inbound WebSockets, so Recall's
    # audio can't reach it directly. A Cloudflare Worker relay accepts the audio
    # WS, runs the Gemini Live session, and POSTs turns back here over HTTP. This
    # is the wss:// base of that relay (e.g. wss://laura-ears.<sub>.workers.dev).
    # Empty = no audio endpoint is attached to the bot (ears effectively off).
    ears_relay_ws_base: str = ""

    # Embeddings for RAG — pick a provider:
    #   hash   (free, offline, zero-dependency keyword vectors)  ← default
    #   local  (free, real semantic embeddings via fastembed)
    #   voyage (best quality, needs VOYAGE_API_KEY)
    embedding_provider: str = "hash"
    voyage_api_key: str = ""
    embedding_model: str = "voyage-3"

    # Recall.ai (live meeting entry: ears + camera)
    recall_api_key: str = ""
    # eu-central-1 is where the real Recall workspace + bots live (prod runs there);
    # override per-workspace with RECALL_API_BASE.
    recall_api_base: str = "https://eu-central-1.recall.ai"
    recall_webhook_secret: str = ""
    # Live transcription provider for Recall bots:
    #   recallai   = fastest built-in path, but low-latency mode is English-only
    #              (accuracy mode does Italian but is minutes late — dead for live)
    #   deepgram   = nova-3 streaming, language detection + code-switching
    #              (Italian/English mixed). API key + project id go in the
    #              RECALL DASHBOARD (eu-central-1), not in our env.
    #   elevenlabs = scribe realtime, multilingual, needs the EL plan to cover it
    recall_transcription_provider: str = "recallai"
    deepgram_model: str = "nova-3"
    deepgram_language: str = "multi"
    recall_transcription_mode: str = "prioritize_low_latency"
    recall_transcription_language_code: str = "en"
    elevenlabs_transcription_model: str = "scribe_v2_realtime"
    elevenlabs_transcription_language_code: str = ""

    # Calendar auto-join (Google OAuth -> Recall Calendar V2)
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""
    google_calendar_redirect_uri: str = ""
    calendar_oauth_state: str = ""
    calendar_invite_emails: str = "laura.ai.122222@gmail.com"

    # Gmail watcher: auto-join when Laura is added to a live Meet via "Add people"
    # (Google emails her the link — no calendar event is created). Needs the
    # gmail.readonly scope on the same Google OAuth. The refresh token is read from
    # here if set, otherwise from the connected Recall calendar.
    google_refresh_token: str = ""
    gmail_watch_enabled: bool = True
    gmail_poll_seconds: float = 15.0

    # Reconciliation loop: polls Recall for each active session's bot and
    # finalizes any whose latest status is terminal (done/call_ended/fatal).
    # It is the backstop for the account status-change webhook (which is the
    # ONLY channel that delivers terminal events, and may be un/mis-configured)
    # and it is the ONLY thing that recovers a bot.fatal that never reached the
    # webhook — so it keeps the per-minute Anam/Recall meter from leaking.
    reconcile_enabled: bool = True
    reconcile_poll_seconds: float = 60.0

    # Granola (post-meeting transcript source — optional alternative to Recall
    # for the summary/checklist path; it can't power the live in-call agent)
    granola_api_key: str = ""
    granola_api_base: str = "https://api.granola.ai"

    # Workflow actions (optional — send the follow-up email / post to Slack)
    sendgrid_api_key: str = ""
    mail_from: str = ""
    slack_webhook_url: str = ""

    # Anam (face)
    anam_api_key: str = ""
    anam_avatar_id: str = ""

    # ElevenLabs (voice). When elevenlabs_api_key is set, /tts uses ElevenLabs
    # with-timestamps (real per-word timings -> accurate lip-sync) instead of the
    # free edge-tts fallback.
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_tts_model: str = "eleven_flash_v2_5"
    # Voice used when the primary ELEVENLABS_VOICE_ID can't synthesize (plan
    # tier / licensing / deleted). Default: ElevenLabs stock "Laura".
    elevenlabs_fallback_voice_id: str = "FGY2WhTYpPnrIDTdsKH5"

    # Public URL of this server (Recall must reach our webhook + avatar page)
    public_base_url: str = "http://127.0.0.1:8000"

    # Photoreal avatar (Stage 2): websocket URL of the GPU streaming server
    # (gpu/server.py), e.g. wss://gpu.lauravatar.com/stream. Empty = the
    # photoreal page falls back to a static portrait (still speaks).
    gpu_stream_url: str = ""

    # Meeting-bound GPU runtime (issue #3): when set AND avatar_page="photoreal",
    # the backend starts this EC2 instance when a session begins and stops it
    # gpu_idle_stop_minutes after the last one ends. Keep the box STOPPED (not
    # terminated) for this to work. Needs gpu/iam-backend-gpu-policy.json on the
    # App Runner instance role. Empty = feature off (manual gpu/launch.sh only).
    gpu_instance_id: str = ""
    gpu_aws_region: str = "eu-central-1"
    gpu_idle_stop_minutes: int = 10

    # Runpod meeting-bound GPU (il gemello Runpod del blocco EC2 qui sopra):
    # con RUNPOD_API_KEY + RUNPOD_POD_ID impostati, il pod photoreal si
    # RESUME quando viene invitato un avatar con face: photoreal e si STOPPA
    # runpod_idle_stop_minutes dopo l'ultima sessione. Gate PER-AVATAR
    # (Avatar.page), non globale: Cedric in 3D non accende mai la GPU.
    runpod_api_key: str = ""
    runpod_pod_id: str = ""
    runpod_idle_stop_minutes: int = 10

    # Which avatar page Recall renders as the bot camera:
    #   "talk" = open-source (TalkingHead + free TTS)  ← default   "avatar" = Anam (paid face+voice)
    # "talk" is the default face: no Anam cost, key-free, and what prod runs. Set
    # AVATAR_PAGE=avatar to use the paid Anam face. Both pages use the same
    # {type:"speak"} ws contract, so switching is env-only, no code change.
    avatar_page: str = "talk"

    # Behaviour
    wake_words: str = "laura"
    # Laura tracks the whole meeting silently (MeetingState) regardless of the wake
    # word. The wake word is optional and only gates *speaking*: when
    # require_wake_word is False (default) she answers any groundable question
    # without needing her name — the in-stream SKIP gate + cooldown keep her from
    # interjecting on things she can't ground. Set True to require her name first.
    require_wake_word: bool = False
    speak_cooldown_seconds: float = 8.0
    # LEGACY — dead on the live streaming path. The old decision.passes_confidence()
    # gate was superseded by the in-stream SKIP sentinel (brain.answer_question_stream
    # decides grounding itself). Kept only for per-avatar config back-compat and the
    # decision.py unit tests; changing it has no effect on live meetings.
    min_confidence: float = 0.55
    # answer_question (non-stream: /demo/ask + non-live asks) grounding floor.
    # The model self-reports sufficient_context; on weak retrieval it can mislabel
    # a world-knowledge answer as document-grounded. If the top retrieved chunk
    # scores below this, force sufficient_context=False + drop citations (answer
    # text unchanged). Grounded matches ~0.6-0.75, irrelevant ~0.30 → 0.45 splits
    # them cleanly. Override via env ANSWER_GROUNDING_FLOOR without a redeploy.
    answer_grounding_floor: float = 0.45
    # Autopilot (acts between meetings; every flag defaults OFF — the zero-key
    # demo never sends anything). See backend/app/autopilot.py.
    autopilot_deliver: bool = False        # auto-send artifact email+Slack at finalize
    autopilot_deliver_to: str = ""         # comma-separated recipients
    autopilot_brief: bool = False          # pre-meeting carryover brief email+Slack
    autopilot_brief_to: str = ""           # falls back to autopilot_deliver_to
    autopilot_nudge: bool = False          # periodic Slack digest of open ledger items
    autopilot_nudge_hours: float = 24.0

    # Native Google executor (docs/product/NATIVE-INTEGRATIONS-PLAN.md, "Now"
    # slice): every avatar executes its OWN approved calendar/gmail actions on
    # the org's Google account — natively, INDEPENDENT of Slack/Cedric. ON by
    # default (2026-07-16, owner decision: booking + email must work natively
    # in all agents, separate from Slack). Still fully gated: an action runs
    # only after a human approves it AND the acting avatar's per-avatar `google`
    # toggle is on; needs the Google client id/secret and a completed
    # /oauth/google/connect (calendar.events + gmail.send write scopes). Every
    # vendor call soft-fails to a "failed" receipt, so the zero-key demo (no
    # Google configured) degrades gracefully rather than breaking. Set
    # NATIVE_EXECUTOR=false to fall back to the Cedric-brokered path.
    # See backend/app/executor.py + google_client.py.
    native_executor: bool = True
    # Find-a-time scheduler (backend/app/scheduler.py). With this ON, a finalized
    # meeting's VAGUE scheduling ask ("book 45 min with Ananth next week") gets a
    # free/busy lookup + ranked candidate slots attached as a CalendarProposal, so
    # the approve doors can offer times instead of dropping the action. Gates the
    # PRODUCER only — off the live meeting hot path (runs at finalize / explicit
    # dashboard request). Default OFF: with it off nothing attaches a proposal, so
    # every downstream approve door is byte-identical to today. Needs
    # native_executor ON to actually create the event after a slot is picked.
    scheduler_find_time: bool = False
    # Key for encrypting the per-org Google refresh token at rest (store.py
    # org_oauth, Fernet). Empty ⇒ falls back to session_secret, so tokens are
    # never stored in plaintext even without extra config; with neither set it
    # fails closed (no public-default key). For the executor go-live, set this to
    # a random value held in SSM SecureString (KMS at rest) so it rotates
    # independently of the session cookie key.
    google_token_enc_key: str = ""

    # ── Asana (project system of record — see docs/ASANA.md) ──
    # Personal Access Token for the workspace, single-tenant fallback: a per-org
    # token stored via store.set_org_oauth(org, pat, provider="asana") wins.
    # Empty + no per-org row ⇒ Asana features are silently off (key-free demo
    # untouched). The write path additionally needs NATIVE_EXECUTOR on.
    asana_token: str = ""
    # The workspace to operate in. Empty ⇒ auto-discovered from the token
    # (most PATs see exactly one workspace).
    asana_workspace_gid: str = ""
    # Auto-push: with this ON, typed asana.* actions are executed at finalize
    # WITHOUT waiting for dashboard approval (the receipt still lands in the
    # same provenance channel). OFF by default — approval-gated is the safe
    # default; this is the one-toggle "make it automatic" switch.
    asana_auto_execute: bool = False
    # OAuth app for the dashboard's one-click "Connect Asana" button: create an
    # app at https://app.asana.com/0/my-apps with redirect URL
    # {PUBLIC_BASE_URL}/oauth/asana/callback and paste its credentials here.
    # Unset ⇒ the Connections card falls back to the paste-a-PAT flow.
    asana_client_id: str = ""
    asana_client_secret: str = ""

    # Proactive intervention (the differentiator): flag ONE missing step as the
    # meeting wraps up. Conservative — needs a higher confidence bar, fires once.
    proactive_enabled: bool = True
    proactive_min_confidence: float = 0.7
    # Closing fallback (DEMO-READY-ROADMAP §5 item 12): the proactive wrap-up AND
    # the quiet-participant nudge fire only when detect_closing()'s regex matches
    # an exact wrap-up phrase. This ADDS a second trigger (never replaces the
    # regex) so both facilitation beats also fire on a natural end-of-meeting
    # LULL: when the room has been idle ≥ closing_fallback_idle_seconds since the
    # last substantive line AND the meeting has run ≥ closing_fallback_min_
    # meeting_seconds. Both gates must hold, so it never fires early in a short or
    # actively-talking call; the beats' own one-shot flags + confidence bar still
    # apply. Conservative defaults (25s lull after a ≥3-min meeting); lower them
    # via env for a short demo, or set the bool False to keep regex-only.
    closing_fallback_enabled: bool = True
    closing_fallback_idle_seconds: float = 25.0
    closing_fallback_min_meeting_seconds: float = 180.0
    # General intelligence on the live path:
    #  - questions that ask for fresh/web info route to Claude's NATIVE web_search
    #    tool (not Groq) on live_search_model — Haiku by default for a low-latency
    #    spoken answer, and never subject to Groq's rate limits. Needs
    #    ANTHROPIC_API_KEY.
    #  - retrieved doc context is only injected when it actually matches the
    #    question (score >= rag_min_context_score), so general questions get
    #    the model's own knowledge instead of doc-quoting.
    live_search_enabled: bool = True
    # Haiku (not Sonnet) for web search: much faster on the live spoken path, still
    # answers current-info questions well. Set to claude-sonnet-5 for deeper search.
    live_search_model: str = "claude-haiku-4-5"
    # 0.28: real process/SFF questions score 0.6+, unrelated chatter ~0.1 —
    # below the bar she answers from her own intelligence, no doc flavor.
    rag_min_context_score: float = 0.28
    # Honest caveat on ungrounded PROCESS answers: when the retrieved chunks were
    # cleared for being below rag_min_context_score AND the question reads as
    # company/process-specific (a lightweight lexical heuristic —
    # brain._looks_process_specific: possessives like "our/my" + process/policy
    # nouns like "policy/process/SOP/procedure/onboarding/refund…", EN + IT), she
    # PREFACES the answer with a brief honest caveat ("I don't see this in your
    # process docs, so answering generally —") instead of presenting world
    # knowledge as if it came from their docs. Only this below-floor + process-
    # specific case is caveated: a general/world question ("capital of France")
    # answers normally, and the grounded (above-floor) happy path is untouched
    # (and pays zero extra latency — the heuristic runs only when below floor).
    # False = today's behaviour (world-knowledge answer, no caveat).
    caveat_ungrounded_process_answers: bool = True
    # Spoken acknowledgment the instant she's addressed by name, while the
    # answer generates — kills the dead air that reads as lag.
    ack_enabled: bool = True

    # Conversation quality: stop speaking the moment a human talks over her
    # (barge-in), and never repeat the same spoken line within the window.
    barge_in_enabled: bool = True
    repeat_suppress_seconds: float = 120.0
    # Backchanneling (Retell-style presence): a tiny "Mm-hm." while a human is
    # mid-monologue, so she reads as listening instead of frozen. Deliberately
    # rare — long utterances only, one per gap window, never while she speaks
    # or right after she spoke.
    backchannel_enabled: bool = True
    backchannel_min_words: int = 25
    backchannel_gap_seconds: float = 45.0

    # Voice command to dismiss her ("Laura, you can leave"): say goodbye, then
    # end the session exactly like a natural meeting end (bot leaves, artifact
    # is built, billing stops). Grace delay lets the goodbye audio finish
    # playing in the meeting before the bot disconnects.
    leave_on_command: bool = True
    leave_grace_seconds: float = 2.5

    # Multi-party turn-taking: on a line NOT addressed to her by name, wait
    # this long before answering — if a human starts talking meanwhile, she
    # yields silently (humans get first right of reply to room-open
    # questions). Direct asks by name are never deferred. 0 disables.
    #   Trimmed 1.8 → 1.2 (2026-07-13, DEMO-READY-ROADMAP §5 item 11): the wait
    #   is DEAD AIR before generation even starts, so an unprompted contribution
    #   was landing ~3-4s after the human stopped (reads as "slow"). This is the
    #   base the adaptive sizer below scales from — a pure latency cut, the
    #   post-sleep yield check is unchanged so barge-in/yield are untouched. The
    #   mid-thought / active-partial branches still stretch to deference_max, so
    #   the classic false-start (jumping a held floor) stays covered. Env-tunable
    #   (DEFERENCE_SECONDS) — raise it to defer harder, 0 to disable the wait.
    deference_seconds: float = 1.2
    # Adaptive deference: when enabled, the deference wait is SIZED (not decided)
    # by context instead of the one fixed `deference_seconds` compromise — a
    # mid-utterance human partial lengthens it toward max, a single-human room
    # shortens it toward min, a room-open question splits the difference. It only
    # tunes the wait duration; the post-sleep yield check is unchanged, so it can
    # never emit unaddressed or double speech. A single explicit boolean gates
    # it so enable/disable via env is one atomic op (no coupled min<max toggling).
    #   Default ON (2026-07-10, owner's realism push): the shorten branches are
    #   guarded by end_of_turn.completeness — she only answers FAST when the
    #   line clearly sounds finished, and always waits LONG when it sounds
    #   mid-thought, so the classic false-start (jumping into a pause) is
    #   covered by the strongest signal, not just the room's shape. Set the
    #   boolean to false to restore the fixed 1.8s wait in one env op.
    #   Keep `deference_active_partial_seconds` strictly below the MINIMUM
    #   observed Recall endpoint lag so the extend branch never fires on the
    #   speaker's own trailing partial (which would inflate every turn to max).
    deference_adaptive_enabled: bool = True
    deference_min_seconds: float = 1.0
    deference_max_seconds: float = 2.6
    deference_active_partial_seconds: float = 0.6
    # Engaged follow-up: a question arriving within this window after SHE
    # spoke is almost always a follow-up to her answer — it bypasses the
    # cooldown and the deference wait (dialogue context is a first-class
    # addressee signal). 0 disables.
    followup_window_seconds: float = 15.0
    # Footing: greet a participant who joins an already-running meeting, and
    # nudge one silent participant once as the meeting wraps up.
    greet_joiners: bool = True
    quiet_nudge_enabled: bool = True
    # Opening settle-in ("wait to be called"): for this long after she joins she
    # stays silent UNLESS directly addressed by name — no joiner greetings, no
    # unprompted room-open answers — so she never talks over the room while it
    # settles (hellos, "can you hear me?", late joiners). The window ends early
    # the instant she's first addressed by name; after it, normal proactive
    # behaviour resumes. 0 disables (revert to speaking from the first line).
    opening_grace_seconds: float = 45.0
    # First-call activation: the opening grace NEVER expires on its own — she
    # stays silent (no unprompted answers, greetings, backchannels, or wrap-up
    # interventions) until someone says her name once ("Laura, come stai?").
    # Being named once activates her for the rest of the meeting. False reverts
    # to the time-boxed grace above.
    first_call_required: bool = True
    # One-time self-introduction (fixes the "joined-but-mute first call"):
    # first_call_required keeps her a SILENT guest until someone says her name,
    # so a first-time room where nobody knows to call her by name gets a joined-
    # but-mute avatar with no cue how to activate her — a poor first impression.
    # This does NOT touch the etiquette (she still WAITS to be addressed for real
    # answers): once, shortly after she is proven to be in the call (the first
    # transcript webhook), she says ONE short line introducing herself and telling
    # the room how to call her in — then goes back to waiting. Fires at most once
    # per session, only if she hasn't already been addressed or spoken (the
    # meeting activating her first makes the intro moot), and is scheduled OFF the
    # live hot path (a detached delayed task, never inline). False = exactly
    # today's behaviour (silent until named). The delay lets the room settle
    # (hellos, "can you hear me?") before she introduces herself.
    self_introduce_on_join: bool = True
    self_introduce_after_seconds: float = 10.0
    # She must never barge in OVER a human to introduce herself (that would be
    # the exact talk-over the etiquette avoids). After the settle-in delay, if a
    # human is audibly mid-utterance (a human partial landed within
    # interject_min_pause_seconds), she re-polls for a natural pause every ~2s and
    # introduces at the FIRST open floor — up to this cap, after which she gives
    # up silently (the moment has passed). The suppression check
    # (addressed_once / she spoke) is re-run each loop, so a room that engages her
    # during the wait aborts the intro entirely.
    self_introduce_max_wait_seconds: float = 40.0
    # Hand-raise etiquette: once activated, when the room is talking among
    # itself (nobody addressed her) and she has a grounded contribution, she
    # does NOT speak over the conversation — she raises her hand (gesture on
    # her /talk tile + a meeting-chat line) and waits to be invited ("dimmi,
    # Laura"). Only in a genuinely crowded room (min_humans); with three or
    # fewer people in the meeting she answers directly as before. The hand
    # lowers silently after timeout_seconds if nobody invites her (the moment
    # has passed).
    hand_raise_enabled: bool = True
    # Owner rule (2026-07-14): raise the hand ONLY when the meeting has MORE than
    # 3 participants. ``roster`` counts humans (Laura is the bot, not in it), so
    # >3 participants incl. Laura == roster >= 3 humans. Below that the room is
    # small enough to just answer. Override via HAND_RAISE_MIN_HUMANS.
    hand_raise_min_humans: int = 3
    hand_raise_timeout_seconds: float = 120.0
    # Motivation gate (decision.should_raise_hand): the SKIP gate decides if a
    # contribution is grounded; these decide if raising the hand for it is
    # socially worth it. Cap per meeting, minimum gap between raises, and a
    # longer back-off after a raise the room ignored — silence means "not now".
    hand_raise_max_per_meeting: int = 4
    hand_raise_min_gap_seconds: float = 90.0
    hand_raise_ignored_gap_seconds: float = 240.0
    # High-confidence interjection escape (DEMO-READY-ROADMAP §5 item 10 — the
    # "wow fires reliably" fix). The marquee multi-person moment is otherwise
    # DOUBLE-gated: she's silent until named, and every unaddressed grounded
    # point becomes a SILENT raised hand the room must notice + invite (lost on
    # the 120s timeout). When her unaddressed contribution is STRONGLY grounded
    # AND the floor is open (the line that opened it sounds finished and no human
    # is audibly mid-utterance), she says ONE grounded line directly instead of
    # raising a silent hand. LOWER-confidence points keep the safe raised-hand
    # default. Reuses the grounding confidence the answer path ALREADY computes —
    # the top retrieval-chunk score, exactly what rag_min_context_score /
    # answer_grounding_floor already threshold on — so there is NO extra LLM call.
    hand_raise_interject_when_confident: bool = True
    # On the SAME 0..1 scale as the retrieval score. IMPORTANT calibration note:
    # the model's in-stream SKIP gate is the PRIMARY "is this grounded + worth
    # saying" decision — a contribution only reaches this bar once SKIP passed
    # and chunks cleared rag_min_context_score. This bar is the SECONDARY "strong
    # enough to interject vs. politely raise a hand" ranker. Default 0.45 mirrors
    # answer_grounding_floor — the codebase's own "this answer is GENUINELY
    # document-grounded" line — so she only interjects when grounding clears the
    # same standard the system trusts a citation on. Measured on the key-free
    # `hash` default (the live path augments the query with history): grounded
    # flagship contributions land ~0.42-0.53, so 0.45 fires the marquee beat
    # out-of-the-box while weaker grounded points keep the safe raised hand. A
    # literal 0.85 would be UNREACHABLE with hash embeddings → the escape would be
    # dead code (why this deviates from the roadmap's illustrative 0.85). Raise it
    # (semantic/voyage embeddings score higher), lower it, or set it above 1.0 to
    # disable interjection — all via env (HAND_RAISE_INTERJECT_MIN_CONFIDENCE), no
    # redeploy. Setting the bool above to False disables it outright.
    hand_raise_interject_min_confidence: float = 0.45
    # The floor must be genuinely OPEN before she interjects a spoken line (both
    # env-tunable). These are a HARD talk-over gate, deliberately stricter than
    # the deference wait-sizing knobs:
    #  - min completeness of the line that opened the floor. 0.6 (not 0.5 — per
    #    end_of_turn.py 0.5 reads as "can't tell", not "finished"): she only
    #    interjects after a line that clearly sounds DONE.
    #  - min silence since the last human partial. 1.0s (larger than the 0.6s
    #    deference_active_partial_seconds, which is calibrated for sizing a wait,
    #    not for a talk-over decision): a full second of no one talking.
    interject_min_completeness: float = 0.6
    interject_min_pause_seconds: float = 1.0
    # Cross-talk / locked-dyad suppression (decision.in_locked_dyad): when two
    # humans are in a tight back-and-forth, an unaddressed interjection reads as
    # butting in — hold it back to a SILENT raised hand and wait longer instead.
    # Suppression-only (never speaks). Default OFF like every comparable knob.
    cross_talk_suppression_enabled: bool = False
    cross_talk_min_turns: int = 4
    cross_talk_max_gap_seconds: float = 8.0
    cross_talk_window: int = 6
    # Talk-over guard for the interjection escape: in hand_mode the WHOLE
    # contribution is generated (deference sleep + full answer collected — several
    # seconds) BEFORE the floor-open check. `interjection_floor_open` judged the
    # floor purely from `last_human_partial_at`, which is written only on
    # transcript PARTIALS (they lag) — so by the time she is ready to speak a
    # human may already have taken the floor and she talks over them. With this
    # ON (default) the floor decision re-checks at SPEAK time with two extra
    # "someone is (or just was) talking" signals: (a) a new transcript line landed
    # while she was generating (transcript grew since the turn started), and (b) a
    # human partial arrived at ANY point during her generation window (not just in
    # the last `interject_min_pause_seconds`) — the longer the generation ran, the
    # wider this catch. Either → she DEFERS (raises the hand instead of speaking
    # over). A genuinely open floor (no new line, no partial during generation)
    # still interjects, so this never makes her silent — it only drops the
    # talk-over cases. False = exactly today's behaviour (single trigger-time
    # reading).
    interject_recheck_floor_at_speak: bool = True

    # Vendor subscription/credit watchdog (vendor_health.py): daily sweep of
    # ElevenLabs characters, Google refresh token, Recall/LLM keys, RunPod
    # balance — non-ok items go to SLACK_WEBHOOK_URL. The checks also serve
    # GET /health/vendors on demand. Costs one cheap HTTP call per vendor/day.
    vendor_alerts_enabled: bool = True
    vendor_check_hours: float = 24.0

    # Server
    host: str = "127.0.0.1"
    port: int = 8000

    # ── Production hardening (backend/app/security.py) ──
    # In-process, per-IP rate limiting on the PUBLIC, EXPENSIVE, UNAUTHENTICATED
    # endpoints only (demo/direct-web-avatar brain + TTS). It NEVER touches the
    # live-meeting path (/webhooks/recall, ws/<id>, /avatar/*, /sessions/*) or
    # the authenticated dashboard — latency is the product there and the contract
    # is sacred. Anti cost-bomb: /demo/post_meeting calls Sonnet-5 per request,
    # the others hit an LLM or ElevenLabs, and anyone can hammer them today.
    # Counts are in-process (fine: prod runs a single App Runner instance,
    # MaxSize=1). Master switch defaults ON but can be flipped OFF via env so the
    # limiter can never wedge a demo. Limits are per-IP, per-endpoint, per window.
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: float = 60.0
    # Sonnet-5 per call — the strictest bucket. A human demoing clicks it a
    # handful of times a minute; a script hammering the summary blows past it.
    rate_limit_demo_post_meeting: int = 6
    # Cheaper grounded Q&A (RAG + fast model). Generous for a live demo.
    rate_limit_demo_ask: int = 30
    # Direct-web-avatar streaming brain (SSE). A real back-and-forth is a few
    # turns a minute; 60 leaves plenty of headroom before it reads as abuse.
    rate_limit_live_ask: int = 60
    # Tool-using answer path (round-trip per call, can hit web search).
    rate_limit_live_act: int = 30
    # Page-side TTS fallback for the /talk avatar voice — GENEROUS on purpose:
    # the live meeting attaches audio server-side (never via this HTTP route),
    # so this is the direct web page + fallback. Kept high so a real talking
    # page is never throttled; still caps a script scraping ElevenLabs credits.
    rate_limit_tts: int = 120

    # Security response headers (security.py middleware). HSTS + nosniff +
    # Referrer-Policy + cross-domain-policies on every response. NOTE: no
    # X-Frame-Options / CSP frame-ancestors — Recall renders the avatar page
    # (/talk, /avatar, /photoreal, /live, /join) in an IFRAME as the bot camera,
    # so frame-blocking would break the live avatar. See security.py.
    security_headers_enabled: bool = True
    # HSTS max-age in seconds (default 2 years). Only honored over HTTPS, so the
    # local http demo is unaffected. Set 0 to omit the HSTS header entirely.
    hsts_max_age_seconds: int = 63072000

    # ── Cedric integration (docs/ in the Cedric X Laura project) ──
    # Avatar used when a session/dispatch doesn't name one explicitly.
    default_avatar_id: str = "laura"
    # INTERNAL avatar folders (comma-separated ids): personas that exist as
    # folders but are not a product surface — excluded from every roster
    # (/avatars, avatars.list_ids/list_for_org, dashboard) and REFUSED by
    # session dispatch for every caller (404 unknown avatar). Backend
    # defense-in-depth for the self-serve launch: even while the folder exists
    # (its removal is a separate track), the internal persona can never be
    # listed or dispatched. Empty = no internal avatars.
    internal_avatar_ids: str = "duccio"
    # Static bearer token for the session API (/sessions/*, /ledger). Empty =
    # open (preserves the zero-key local demo); set in any real deployment.
    laura_api_token: str = ""
    # ── Multi-tenancy spine (docs/infra/MULTI-TENANCY*.md) ──
    # Runtime control-plane database. Empty = the key-free SQLite demo. In
    # production this MUST be the dedicated `laura_app` pooler credential
    # (NOSUPERUSER, NOBYPASSRLS); App Runner receives this URL and never the
    # migration owner credential. The hot path stays on local SQLite.
    laura_database_url: str = ""
    # Migration/DDL credential used ONLY by backend/alembic/env.py and one-off
    # migration jobs. Never inject this owner URL into App Runner. Keeping a
    # separate setting makes accidentally running the service with BYPASSRLS
    # structurally harder.
    laura_database_admin_url: str = ""
    # Migration jobs set this to true so a missing admin secret is fatal even
    # when the runtime URL is intentionally absent from that job.
    laura_require_migrations: bool = False
    # The fixed tenant every unauthenticated / service / anon row is stamped
    # with. Its uuid is seeded as the "Demo" org row so the single-tenant demo
    # stays byte-identical while every persisted row still carries a non-null
    # org_id (the "start right so we never re-architect" invariant, §0).
    demo_org_id: str = "00000000-0000-0000-0000-0000000000de"
    # ── tenancy policy ────────────────────────────────────────────────────
    # Personal-first (owner decision 2026-07-13, re-confirmed 2026-07-16):
    # every login provisions ONE personal durable org — including logins on a
    # VERIFIED corporate domain. Set true to restore domain→shared-org routing
    # (the parked "teams" behavior). Honored by BOTH resolvers: the SQLite
    # org_id_for_email mirror and the Postgres laura_private.ensure_user
    # (synced at boot via control_plane.sync_policy_flags — a SQL function
    # cannot read a process env var). Env: LAURA_SHARED_DOMAIN_ORGS (the field
    # is not laura_-named, so an explicit alias binds the documented var; the
    # bare SHARED_DOMAIN_ORGS is accepted too).
    shared_domain_orgs: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "LAURA_SHARED_DOMAIN_ORGS", "SHARED_DOMAIN_ORGS"
        ),
    )

    # ── usage metering / the free entitlement (backend/app/entitlements.py) ──
    # Only active when laura_database_url is set (the durable control plane);
    # the key-free demo has NO metering and NO enforcement. Seconds of included
    # avatar time a brand-new free org gets (the "15 free minutes"). Used as the
    # default included_seconds when the billing row is created.
    free_trial_seconds: int = 900
    # Included seconds for the paid solo plan (PR C's checkout flips
    # billing_accounts to plan='solo' with this allowance — 300 min/month).
    solo_included_seconds: int = 18000
    # Stripe Billing is deliberately fail-closed. Production stays key-free and
    # unchanged until every value below is configured and the runtime database
    # proves it is the least-privileged laura_app role.
    billing_enabled: bool = False
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_solo: str = ""
    stripe_portal_configuration_id: str = ""
    # False means Stripe test mode; True means live mode. The key and every
    # signed webhook event must agree with this explicit switch.
    stripe_live_mode: bool = False
    stripe_api_version: str = "2026-02-25.clover"
    stripe_webhook_max_body_bytes: int = 262144
    billing_checkout_reservation_seconds: int = 300
    # Spoken heads-up near the usage deadline (~5 min and ~1 min before the
    # avatar must leave). Fires from the reconcile pass — never the hot path.
    usage_warnings_enabled: bool = True
    # Seed the first REAL org (SFF Studio) + its verified domain + agent grants
    # at store init, so the org_id seam is actually exercised (a member of that
    # org sees only its granted avatars). Idempotent, safe to run every boot —
    # the store is ephemeral and re-seeds on redeploy. Set False to disable
    # (a test toggles it to assert the un-seeded fallback).
    seed_builtin_orgs: bool = True
    # HMAC key for signing dashboard login cookies (auth.py). Empty = a random
    # per-boot key is derived, which just means users re-login after a restart
    # — fine for now, set a stable value in a real deployment.
    session_secret: str = ""
    # Who may sign in to the dashboard. Comma-separated allowlist of exact
    # emails and/or "@domain" suffixes. Empty = allow any Google account that
    # can reach the consent screen (in OAuth "testing" mode Google already
    # restricts that to configured test users). Set it for a locked deployment.
    dashboard_allowed_emails: str = ""
    # HMAC key for signing callbacks POSTed to a session's callback_url
    # (X-Laura-Signature: t=<ts>,v1=<hex>). Shared with the orchestrator.
    laura_webhook_secret: str = ""
    # Per-client signing registry: a JSON object {org_id: secret}. When a
    # session belongs to a connected org, its callbacks are signed with the
    # org's own secret (minted at Connect-the-brain provisioning); orgs not in
    # the map — and service starts — fall back to LAURA_WEBHOOK_SECRET.
    laura_webhook_secrets_by_org: str = ""
    # SSM is the durable source of truth for that registry.  The env value
    # above remains the boot-time fallback; successful provisioning merges the
    # new org into this SecureString and refreshes the in-process cache.
    laura_webhook_registry_ssm_parameter: str = (
        "/laura/prod/LAURA_WEBHOOK_SECRETS_BY_ORG"
    )
    laura_webhook_registry_refresh_seconds: float = 300.0
    laura_webhook_registry_aws_region: str = "eu-central-1"
    # Connect-the-brain provisioning: the orchestrator's org endpoint (Cedric's
    # /api/laura/orgs) and the bearer it expects. Unset → a brain connection
    # saves locally as "pending" (the dashboard says so); the call goes live
    # the moment Cedric ships the route.
    cedric_orgs_url: str = ""
    cedric_orgs_token: str = ""
    # Programmatic tool bridge (Handshake contract v3, 2026-07-16): consume
    # Cedric's connected tools as an MCP server (POST /api/laura/mcp). OFF by
    # default — flip to true ONLY once Cedric confirms their endpoint is live.
    # When off, nothing calls Cedric's MCP surface and session-start/live paths
    # are byte-identical to today.
    cedric_mcp_enabled: bool = False
    # Hard client-side budget for a LIVE-meeting tool call (the contract pins
    # read+fast tools only on the hot path; this enforces it defensively).
    cedric_mcp_live_timeout_s: float = 2.0
    # Timeout for OFF-path tool calls (dashboard / post-meeting); the contract
    # allows worst-case ~60s tools, so the client waits up to this.
    cedric_mcp_offpath_timeout_s: float = 90.0
    # Bearer presented on those callbacks (the orchestrator's cheap first-line
    # check before HMAC verification).
    laura_webhook_token: str = ""
    # Bearer presented when fetching a session's context_url at join time.
    laura_context_token: str = ""
    # LIVE context feed: re-pull the session's context_url whenever the last
    # pull is older than this many seconds (transcript-driven, off the live
    # path) — the brief stays current for the WHOLE meeting instead of being a
    # join-time snapshot. 0 = the old one-shot join-time pull only.
    context_refresh_seconds: float = 120.0
    # Per-attempt timeout for callback/context HTTP calls.
    callback_timeout_seconds: float = 10.0
    # Model A default routing: a DEFAULT callback_url for sessions that don't
    # supply their own (email/calendar/API summons) → every meeting hands its
    # artifact + live actions to Cedric's receiver, and Cedric does the Slack
    # posting + execution with his own tools. Set = Model A; unset = Laura runs
    # autonomously (Model B: EXECUTE_* / autopilot). Point at Cedric's
    # {PUBLIC_BASE_URL}/api/laura/events.
    surface_webhook_url: str = ""
    # Pre-meeting context pull (Cedric → Laura): default context_url for meetings
    # that don't set their own → the avatar fetches "who's who + context" from
    # Cedric's memory at join. Point at Cedric's {PUBLIC_BASE_URL}/api/laura/context.
    surface_context_url: str = ""
    # Default external_ref (JSON) for sessions that don't carry their own —
    # e.g. '{"team":"T1","slack_channel":"#cedric","requested_by":"duccio"}'.
    # Without it, email/dashboard-summoned meetings reach the orchestrator with
    # external_ref {} and it has no Slack channel to route cards/recaps to
    # (live finding 2026-07-10). A real Cedric summon's own external_ref wins.
    surface_external_ref: str = ""
    # ── end Cedric integration ──

    @property
    def execution_mode(self) -> str:
        """Which engine runs an APPROVED meeting action — the single settings
        concept the dashboard reads to show "who executes" (NATIVE-INTEGRATIONS-
        PLAN.md "Cedric add-on toggle"). For the Now slice this is derived from
        the native_executor flag: "native" = Laura runs calendar/gmail on the
        user's own Google account; "cedric" = today's behaviour, an approved
        action is brokered to Cedric. A real per-org setting can override this
        property later without changing call sites."""
        return "native" if self.native_executor else "cedric"

    @property
    def wake_word_list(self) -> list[str]:
        """Global fallback wake words (per-avatar wake_words usually win)."""
        return [w.strip().lower() for w in self.wake_words.split(",") if w.strip()]

    @property
    def internal_avatar_id_set(self) -> set[str]:
        """Parsed INTERNAL_AVATAR_IDS — folder ids hidden from every roster and
        refused by dispatch (see the field's comment above)."""
        return {
            a.strip().lower()
            for a in self.internal_avatar_ids.split(",")
            if a.strip()
        }

    @property
    def avatars_dir(self) -> Path:
        return REPO_ROOT / "avatars"


settings = Settings()
