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

    # Ollama (only used when BRAIN_PROVIDER=ollama)
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

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

    # Granola (post-meeting transcript source — optional alternative to Recall
    # for the summary/checklist path; it can't power the live in-call agent)
    granola_api_key: str = ""
    granola_api_base: str = "https://api.granola.ai"

    # Anam (face)
    anam_api_key: str = ""
    anam_avatar_id: str = ""

    # ElevenLabs (voice)
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""

    # Public URL of this server (Recall must reach our webhook + avatar page)
    public_base_url: str = "http://127.0.0.1:8000"

    # Behaviour
    wake_words: str = "lucas"
    speak_cooldown_seconds: float = 8.0
    min_confidence: float = 0.55

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
