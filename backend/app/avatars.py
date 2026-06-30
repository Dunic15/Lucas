"""Avatar registry.

Sofia is a folder under `avatars/sofia/` containing:
  - avatar.yaml   (the editable config)
  - knowledge/    (markdown process docs)

This module turns Sofia's folder into an `Avatar` object the rest of the code
uses. It keeps the folder-based shape so a second agent can be added later
without changing the meeting flow.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import settings


@dataclass
class Avatar:
    id: str
    name: str
    role: str
    wake_words: list[str]
    persona_prompt: str
    anam_avatar_id: str
    anam_avatar_model: str
    anam_voice_id: str
    min_confidence: float
    speak_cooldown_seconds: float
    dir: Path

    @property
    def knowledge_dir(self) -> Path:
        return self.dir / "knowledge"

    @property
    def index_path(self) -> Path:
        return self.dir / ".index.json"


def _coalesce(value, fallback):
    """yaml blank fields parse to None/'' — fall back to the global default."""
    return fallback if value in (None, "") else value


def load(avatar_id: str) -> Avatar:
    folder = settings.avatars_dir / avatar_id
    cfg_path = folder / "avatar.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"No avatar '{avatar_id}' (expected {cfg_path}). "
            f"Available: {', '.join(list_ids()) or 'none'}"
        )

    raw = yaml.safe_load(cfg_path.read_text()) or {}
    wake = [str(w).lower() for w in (raw.get("wake_words") or [avatar_id])]

    return Avatar(
        id=raw.get("id", avatar_id),
        name=raw.get("name", avatar_id.title()),
        role=raw.get("role", "AI Process Expert"),
        wake_words=wake,
        persona_prompt=(raw.get("persona_prompt") or "").strip(),
        anam_avatar_id=_coalesce(raw.get("anam_avatar_id"), settings.anam_avatar_id),
        anam_avatar_model=_coalesce(
            raw.get("anam_avatar_model"), settings.anam_avatar_model
        ),
        anam_voice_id=_coalesce(raw.get("anam_voice_id"), settings.anam_voice_id),
        min_confidence=float(_coalesce(raw.get("min_confidence"), settings.min_confidence)),
        speak_cooldown_seconds=float(
            _coalesce(raw.get("speak_cooldown_seconds"), settings.speak_cooldown_seconds)
        ),
        dir=folder,
    )


def list_ids() -> list[str]:
    root = settings.avatars_dir
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "avatar.yaml").exists())
