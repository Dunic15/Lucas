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

    # Reasoning
    anthropic_api_key: str = ""
    brain_model: str = "claude-sonnet-4-6"

    # Embeddings
    voyage_api_key: str = ""
    embedding_model: str = "voyage-3"

    # Recall.ai
    recall_api_key: str = ""
    recall_api_base: str = "https://us-west-2.recall.ai"

    # Tavus (face)
    tavus_api_key: str = ""
    tavus_replica_id: str = ""
    tavus_persona_id: str = ""

    # ElevenLabs (voice)
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""

    # Public URL of this server (Recall must reach our webhook + avatar page)
    public_base_url: str = "http://127.0.0.1:8000"

    # Behaviour
    wake_words: str = "sofia"
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
