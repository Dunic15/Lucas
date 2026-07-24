"""ElevenLabs Agents conversation runtime — Cedric-only pilot (PR 1: routing).

An avatar on this runtime hands its live-meeting CONVERSATION (STT, turn
detection, interruption handling, LLM reply, streaming voice) to a private
ElevenLabs Agent behind the Cloudflare relay, while knowledge and actions stay
in this backend (client tools -> Company Brain / queue_action / approvals).
Every other avatar keeps the legacy pipeline untouched. Design + rollout:
docs/product/CEDRIC-ELEVENLABS-PILOT.md.

This module is the ONE authority on which runtime an avatar gets. Dispatch
requires ALL of (fail-closed to "legacy"):

  1. settings.elevenlabs_agent_runtime_enabled  — global kill switch, default OFF
  2. avatar id in settings.elevenlabs_agent_avatar_allowlist ("cedric")
  3. avatar.yaml conversation_runtime: elevenlabs_agent
  4. a non-empty avatar.yaml elevenlabs_agent_id

so no single config mistake (a flipped env var, a copy-pasted yaml block, a
widened allowlist) can move Laura or Petra off the legacy path. The resolved
runtime is FROZEN onto the session at creation (store.create) — a yaml edit
mid-meeting never migrates a live call; after a backend restart the in-memory
snapshot deliberately falls back to "legacy" (the relay bridge died with the
process, and legacy is the runtime that still works).

PR 1 ships routing only: nothing here opens a connection, and with the flag at
its default this module changes no behavior at all.
"""
from __future__ import annotations

import logging

from ..config import settings

_log = logging.getLogger(__name__)

RUNTIME_LEGACY = "legacy"
RUNTIME_ELEVENLABS_AGENT = "elevenlabs_agent"


def runtime_for_avatar(avatar) -> str:
    """Effective conversation runtime for a loaded Avatar (fail-closed).

    Returns RUNTIME_ELEVENLABS_AGENT only when the global flag, the allowlist,
    the avatar's own yaml choice AND a non-empty agent id all agree; any other
    combination — including any surprise — is the legacy pipeline.
    """
    try:
        if not settings.elevenlabs_agent_runtime_enabled:
            return RUNTIME_LEGACY
        if getattr(avatar, "conversation_runtime", RUNTIME_LEGACY) != RUNTIME_ELEVENLABS_AGENT:
            return RUNTIME_LEGACY
        avatar_id = str(getattr(avatar, "id", "") or "").strip().lower()
        if avatar_id not in settings.elevenlabs_agent_avatar_allowlist_set:
            return RUNTIME_LEGACY
        if not str(getattr(avatar, "elevenlabs_agent_id", "") or "").strip():
            return RUNTIME_LEGACY
        return RUNTIME_ELEVENLABS_AGENT
    except Exception:  # noqa: BLE001 — a resolver crash must never block dispatch
        _log.warning("conversation-runtime resolution failed; using legacy", exc_info=True)
        return RUNTIME_LEGACY


def runtime_for_avatar_id(avatar_id: str) -> tuple[str, str]:
    """(runtime, agent_id) for an avatar id — the session-snapshot form.

    Loads the avatar config itself so callers (store.create) don't need the
    avatars module; ANY failure (missing folder, broken yaml) resolves to the
    legacy pipeline with no agent id, never an exception — session creation
    must be unbreakable by config.
    """
    try:
        from .. import avatars  # lazy: store <-> avatars import cycle

        avatar = avatars.load(avatar_id)
    except Exception:  # noqa: BLE001
        return RUNTIME_LEGACY, ""
    runtime = runtime_for_avatar(avatar)
    if runtime != RUNTIME_ELEVENLABS_AGENT:
        return RUNTIME_LEGACY, ""
    return runtime, str(avatar.elevenlabs_agent_id).strip()
