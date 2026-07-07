"""Avatar registry.

An avatar is a folder under `avatars/<id>/` containing:
  - avatar.yaml   (the editable config)
  - knowledge/    (markdown process docs)

This module turns that folder into an `Avatar` object the rest of the code
uses. Adding an avatar = adding a folder. No code changes. See avatars/README.md.
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
    elevenlabs_voice_id: str
    min_confidence: float
    speak_cooldown_seconds: float
    dir: Path
    # Other avatar folders whose knowledge/ is indexed INTO this avatar too
    # (e.g. laura references the "sff" pack instead of copying its files —
    # real-world packs live in exactly one place).
    knowledge_packs: list[str] = None  # type: ignore[assignment]

    @property
    def knowledge_dir(self) -> Path:
        return self.dir / "knowledge"

    @property
    def knowledge_dirs(self) -> list[Path]:
        dirs = [self.knowledge_dir]
        for pack in self.knowledge_packs or []:
            pack_dir = settings.avatars_dir / pack / "knowledge"
            if pack_dir.exists():
                dirs.append(pack_dir)
        return dirs

    @property
    def index_path(self) -> Path:
        return self.dir / ".index.json"


def _coalesce(value, fallback):
    """yaml blank fields parse to None/'' — fall back to the global default."""
    return fallback if value in (None, "") else value


# Config cache. The live webhook loads the avatar on EVERY transcript event —
# and partial events arrive several times a second while anyone talks — so an
# uncached YAML read is sync disk I/O on the hot path. Keyed by path + mtime:
# an edited avatar.yaml or a freshly scaffolded avatar is picked up without a
# restart, and tests that point avatars_dir elsewhere never collide.
_load_cache: dict[str, tuple[float, Avatar]] = {}


def load(avatar_id: str) -> Avatar:
    folder = settings.avatars_dir / avatar_id
    cfg_path = folder / "avatar.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"No avatar '{avatar_id}' (expected {cfg_path}). "
            f"Available: {', '.join(list_ids()) or 'none'}"
        )

    mtime = cfg_path.stat().st_mtime
    cached = _load_cache.get(str(cfg_path))
    if cached is not None and cached[0] == mtime:
        return cached[1]

    raw = yaml.safe_load(cfg_path.read_text()) or {}
    wake = [str(w).lower() for w in (raw.get("wake_words") or [avatar_id])]

    avatar = Avatar(
        id=raw.get("id", avatar_id),
        name=raw.get("name", avatar_id.title()),
        role=raw.get("role", "AI Process Expert"),
        wake_words=wake,
        persona_prompt=(raw.get("persona_prompt") or "").strip(),
        anam_avatar_id=_coalesce(
            raw.get("anam_avatar_id") or raw.get("tavus_replica_id"),  # back-compat
            settings.anam_avatar_id,
        ),
        elevenlabs_voice_id=_coalesce(
            raw.get("elevenlabs_voice_id"), settings.elevenlabs_voice_id
        ),
        min_confidence=float(_coalesce(raw.get("min_confidence"), settings.min_confidence)),
        speak_cooldown_seconds=float(
            _coalesce(raw.get("speak_cooldown_seconds"), settings.speak_cooldown_seconds)
        ),
        dir=folder,
        knowledge_packs=[str(k) for k in (raw.get("knowledge_packs") or [])],
    )
    _load_cache[str(cfg_path)] = (mtime, avatar)
    return avatar


def list_ids() -> list[str]:
    root = settings.avatars_dir
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "avatar.yaml").exists())
