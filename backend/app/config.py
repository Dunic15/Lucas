"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path

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
    #   ollama    (free, local, needs Ollama running)
    #   stub      (free, offline, no model — deterministic, for a zero-key demo)
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

    # Ollama (only used when BRAIN_PROVIDER=ollama)
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    # Groq (fast, cheap open models via an OpenAI-compatible API).
    # BRAIN_PROVIDER=groq; set BRAIN_MODEL / BRAIN_MODEL_FAST to a Groq model id
    # (e.g. llama-3.3-70b-versatile). ~0.2-0.4s first token, no latency spikes.
    groq_api_key: str = ""
    groq_base: str = "https://api.groq.com/openai/v1"

    # Embeddings for RAG — pick a provider:
    #   hash   (free, offline, zero-dependency keyword vectors)  ← default
    #   local  (free, real semantic embeddings via fastembed)
    #   voyage (best quality, needs VOYAGE_API_KEY)
    embedding_provider: str = "hash"
    voyage_api_key: str = ""
    embedding_model: str = "voyage-3"

    # Recall.ai (live meeting entry: ears + camera)
    recall_api_key: str = ""
    recall_api_base: str = "https://us-west-2.recall.ai"
    recall_webhook_secret: str = ""
    # Live transcription provider for Recall bots:
    #   recallai   = fastest built-in path, but low-latency mode is English-only
    #   elevenlabs = better multilingual/accent handling when configured in Recall
    recall_transcription_provider: str = "recallai"
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

    # Which avatar page Recall renders as the bot camera:
    #   "avatar" = Anam (paid face+voice)   "talk" = open-source (TalkingHead + free TTS)
    # Flip to "talk" (AVATAR_PAGE=talk) once /talk is validated in a browser — no
    # code change, no Anam cost. Both pages use the same {type:"speak"} ws contract.
    avatar_page: str = "avatar"

    # Behaviour
    wake_words: str = "laura"
    # When False, the avatar answers any grounded question without needing to be
    # called by name first (the SKIP gate + cooldown still keep her from spamming).
    require_wake_word: bool = False
    speak_cooldown_seconds: float = 8.0
    min_confidence: float = 0.55
    # Autopilot (acts between meetings; every flag defaults OFF — the zero-key
    # demo never sends anything). See backend/app/autopilot.py.
    autopilot_deliver: bool = False        # auto-send artifact email+Slack at finalize
    autopilot_deliver_to: str = ""         # comma-separated recipients
    autopilot_brief: bool = False          # pre-meeting carryover brief email+Slack
    autopilot_brief_to: str = ""           # falls back to autopilot_deliver_to
    autopilot_nudge: bool = False          # periodic Slack digest of open ledger items
    autopilot_nudge_hours: float = 24.0

    # Proactive intervention (the differentiator): flag ONE missing step as the
    # meeting wraps up. Conservative — needs a higher confidence bar, fires once.
    proactive_enabled: bool = True
    proactive_min_confidence: float = 0.7
    # General intelligence on the live path:
    #  - questions that ask for fresh/web info route to a Groq compound model
    #    with built-in server-side web search (same key, streamed);
    #  - retrieved doc context is only injected when it actually matches the
    #    question (score >= rag_min_context_score), so general questions get
    #    the model's own knowledge instead of doc-quoting.
    live_search_enabled: bool = True
    live_search_model: str = "groq/compound-mini"
    # 0.28: real process/SFF questions score 0.6+, unrelated chatter ~0.1 —
    # below the bar she answers from her own intelligence, no doc flavor.
    rag_min_context_score: float = 0.28
    # Spoken acknowledgment the instant she's addressed by name, while the
    # answer generates — kills the dead air that reads as lag.
    ack_enabled: bool = True

    # Conversation quality: stop speaking the moment a human talks over her
    # (barge-in), and never repeat the same spoken line within the window.
    barge_in_enabled: bool = True
    repeat_suppress_seconds: float = 120.0

    # Server
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def wake_word_list(self) -> list[str]:
        """Global fallback wake words (per-avatar wake_words usually win)."""
        return [w.strip().lower() for w in self.wake_words.split(",") if w.strip()]

    @property
    def avatars_dir(self) -> Path:
        return REPO_ROOT / "avatars"


settings = Settings()
