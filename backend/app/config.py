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
    brain_model: str = "claude-sonnet-4-6"
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
    # Real meetings default to requiring the name: the avatar is one of many
    # voices, so it stays silent unless directly addressed (a wake word) OR asked
    # a clear on-topic question (see decision.is_direct_question). Set
    # REQUIRE_WAKE_WORD=false to fall back to the old answer-anything behaviour.
    require_wake_word: bool = True
    # Silence to keep after speaking before she'll volunteer again. Kept low so a
    # named follow-up feels responsive; being called by name skips it entirely
    # (see the gate in main.py), and the repetition guard stops her repeating.
    speak_cooldown_seconds: float = 4.0
    min_confidence: float = 0.55
    # Proactive intervention (the differentiator): flag ONE missing step as the
    # meeting wraps up. Conservative — needs a higher confidence bar, fires once.
    proactive_enabled: bool = True
    proactive_min_confidence: float = 0.7

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
