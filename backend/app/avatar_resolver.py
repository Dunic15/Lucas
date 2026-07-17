"""The one canonical avatar resolver (M2) — every runtime path goes here.

resolve(org_id, avatar_key, principal_id=None, meeting_context=None) returns a
``ResolvedAvatar``: the immutable canonical ``Avatar`` (repo avatar.yaml)
overlaid with the org's currently PUBLISHED personalization, plus provenance
and the effective capability decisions. Callers never merge avatar data
themselves.

Design constraints honored:

- **Latency**: resolution happens at session start / door / dashboard time.
  The live transcript path reads the ResolvedAvatar stashed on the session —
  never Postgres. An in-process TTL cache (60s, same convergence bound as the
  knowledge indexes) bounds resolver I/O for the non-session callers too.
- **Byte-identical off**: with ``ORG_AVATAR_OVERLAYS_ENABLED=false`` (default)
  or no control plane or no published overlay, ``resolve`` returns the
  canonical avatar wrapped unchanged — same object contents, same behavior.
- **Narrowing only**: effective capabilities are an INTERSECTION — canonical
  ceiling ∩ overlay ``enabled_tools`` — and the per-avatar org toggles plus
  connected-account checks at the execution doors still apply on top
  (permissions are re-resolved at execution time; configuration is never the
  security boundary).
- **Wake safety**: an org display name ADDS a wake word; canonical wake
  words (and therefore stop/leave phrases) are never removed.
- **Prompt safety**: overlay behavioral fields append a bounded, clearly
  delimited block to the canonical persona prompt (avatar_overlay.
  preferences_block) — never a replacement, and retrieved documents stay
  data, never instructions.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from . import avatar_overlay, avatars
from .avatars import Avatar
from .config import settings

_TTL_SECONDS = 60.0
_LOCK = threading.Lock()
# (org_id, avatar_key) -> (expires_at, overlay_row_or_None)
_CACHE: dict[tuple[str, str], tuple[float, Optional[dict]]] = {}


@dataclass
class ResolvedAvatar(Avatar):
    """An Avatar every existing consumer accepts, plus M2 provenance.

    ``context_scope`` rides the object into rag.retrieve (which reads it via
    getattr — no signature changes through brain). ``effective_tools`` is the
    narrowed capability-family set; the execution doors re-check it."""

    overlay_version: int = 0
    overlay_org: str = ""
    assignment_id: str = ""
    effective_tools: tuple = ()
    context_scope: Optional[dict] = None
    overlay_fields: tuple = ()
    resolver_provenance: dict = field(default_factory=dict)


def _canonical_wrapped(canonical: Avatar) -> ResolvedAvatar:
    """The flag-off / no-overlay result: canonical contents, zero changes."""
    return ResolvedAvatar(
        **{k: getattr(canonical, k) for k in canonical.__dataclass_fields__},
        effective_tools=tuple(sorted(
            avatar_overlay.capability_ceiling(canonical)
        )),
        resolver_provenance={"canonical": canonical.id, "overlay": None},
    )


def invalidate(org_id: str = "", avatar_key: str = "") -> None:
    """Drop cached overlays (publish/rollback/enable call this locally; other
    instances converge within the TTL)."""
    with _LOCK:
        if not org_id:
            _CACHE.clear()
            return
        for key in [k for k in _CACHE
                    if k[0] == org_id and (not avatar_key or k[1] == avatar_key)]:
            _CACHE.pop(key, None)


def _cached_overlay(org_id: str, avatar_key: str) -> Optional[dict]:
    now = time.time()
    key = (org_id, avatar_key)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    from . import org_avatars_pg

    try:
        row = org_avatars_pg.current_overlay(org_id, avatar_key)
    except Exception:  # noqa: BLE001 — an overlay read must never break a session
        row = None
    with _LOCK:
        _CACHE[key] = (now + _TTL_SECONDS, row)
    return row


def _apply(canonical: Avatar, org_id: str, overlay_row: dict,
           assignment_id: str = "") -> ResolvedAvatar:
    overlay = overlay_row.get("overlay") or {}
    version = int(overlay_row.get("version") or 0)

    kwargs: dict[str, Any] = {}
    if overlay.get("display_name"):
        kwargs["name"] = overlay["display_name"]
        # ADD the display name as a wake word; never remove canonical ones
        # (stop/leave phrases keep working under any rename).
        extra = overlay["display_name"].strip().lower()
        if extra and extra not in canonical.wake_words:
            kwargs["wake_words"] = [*canonical.wake_words, extra]
    if overlay.get("role"):
        kwargs["role"] = overlay["role"]
    if overlay.get("mission"):
        kwargs["mission"] = overlay["mission"]
    if overlay.get("voice_id"):
        kwargs["elevenlabs_voice_id"] = overlay["voice_id"]
    if overlay.get("face"):
        kwargs["face"] = overlay["face"]
    if overlay.get("talk_body"):
        kwargs["talk_body"] = overlay["talk_body"]

    block = avatar_overlay.preferences_block(overlay)
    if block:
        kwargs["persona_prompt"] = (canonical.persona_prompt or "") + block

    # Capability intersection: canonical ceiling ∩ overlay restriction. An
    # absent enabled_tools means "no restriction"; [] means "no tools".
    ceiling = avatar_overlay.capability_ceiling(canonical)
    restricted = overlay.get("enabled_tools")
    effective = (
        ceiling if restricted is None else (ceiling & set(restricted))
    )
    # native_tools narrows too, so tool exposure derived from the avatar
    # object (registry, prompts) follows the intersection automatically.
    kwargs["native_tools"] = [
        t for t in (canonical.native_tools or []) if t in effective
    ]

    base = replace(canonical, **kwargs)
    return ResolvedAvatar(
        **{k: getattr(base, k) for k in base.__dataclass_fields__},
        overlay_version=version,
        overlay_org=org_id,
        assignment_id=assignment_id,
        effective_tools=tuple(sorted(effective)),
        context_scope=overlay.get("context_scope"),
        overlay_fields=tuple(sorted(overlay)),
        resolver_provenance={
            "canonical": canonical.id,
            "overlay": version,
            "assignment": assignment_id or None,
            "greeting": overlay.get("greeting", ""),
            "followup_prefs": overlay.get("followup_prefs", ""),
        },
    )


def enabled() -> bool:
    from . import org_avatars_pg

    return org_avatars_pg.enabled()


def resolve(
    org_id: str,
    avatar_key: str,
    principal_id: Optional[str] = None,
    meeting_context: Optional[dict] = None,
) -> Avatar:
    """The canonical resolution. Raises FileNotFoundError only for an unknown
    canonical avatar (exactly like avatars.load).

    With no applicable overlay (flag off, non-durable org, nothing published)
    this returns THE SAME cached instance ``avatars.load`` returns — not a
    copy — so identity, the mtime-refresh contract, and flag-off behavior are
    untouched. An applied overlay returns a fresh ResolvedAvatar built with
    dataclasses.replace (the shared cached instance is never mutated)."""
    canonical = avatars.load(avatar_key)
    org = (org_id or "").strip()
    if not org or not enabled():
        return canonical
    from . import control_plane

    if not control_plane.is_durable_org(org):
        return canonical
    row = _cached_overlay(org, avatar_key)
    if row is None:
        return canonical
    return _apply(canonical, org, row)


def describe(org_id: str, avatar_key: str) -> ResolvedAvatar:
    """The provenance-carrying view for the API/preview surfaces: always a
    ResolvedAvatar, even when nothing is overlaid (effective_tools then equal
    the canonical ceiling and overlay is None)."""
    resolved = resolve(org_id, avatar_key)
    if isinstance(resolved, ResolvedAvatar):
        return resolved
    return _canonical_wrapped(resolved)


def resolve_avatar_key(
    org_id: str, *, requested: str = "", principal_id: str = ""
) -> str:
    """Deterministic avatar SELECTION precedence:

        explicit request > user assignment > org default assignment
        > settings.default_avatar_id (canonical default).

    Only consulted when the caller did not name an avatar; assignment rows
    whose avatar no longer exists as a folder fall through safely."""
    if (requested or "").strip():
        return requested.strip()
    org = (org_id or "").strip()
    if org and enabled():
        from . import control_plane, org_avatars_pg

        if control_plane.is_durable_org(org):
            try:
                row = org_avatars_pg.assignment_for(
                    org, principal_id=principal_id or ""
                )
            except Exception:  # noqa: BLE001 — selection must never break dispatch
                row = None
            if row is not None:
                key = str(row.get("avatar_key") or "")
                if key and key in avatars.list_ids():
                    return key
    return settings.default_avatar_id


def family_allowed(org_id: str, avatar_key: str, family: str) -> bool:
    """Execution-time capability re-check for the approve doors: does the
    org's published overlay allow this capability family? Flag off / no
    overlay / any resolver failure ⇒ allowed (today's behavior; the existing
    per-avatar org toggles and connected-account soft-fails still apply)."""
    try:
        resolved = resolve(org_id, avatar_key)
    except Exception:  # noqa: BLE001 — never turn a config read into a 500
        return True
    if not getattr(resolved, "overlay_version", 0):
        return True
    return str(family or "").strip().lower() in resolved.effective_tools


def resolve_for_dispatch(org_id: str, avatar_key: str) -> Avatar:
    """Session-dispatch resolution: the overlaid avatar when the feature is
    on, the canonical avatar otherwise. Never raises beyond avatars.load's
    own FileNotFoundError; any overlay failure degrades to canonical."""
    try:
        return resolve(org_id, avatar_key)
    except FileNotFoundError:
        raise
    except Exception:  # noqa: BLE001 — dispatch must never break on config reads
        return avatars.load(avatar_key)


def for_session(session) -> Avatar:
    """The avatar for a LIVE session — hot-path safe: returns the resolved
    avatar stashed at dispatch, else the canonical mtime-cached load. NEVER
    performs database I/O (a restarted instance mid-meeting falls back to
    canonical behavior — the same frozen-at-dispatch model as mission)."""
    stashed = getattr(session, "resolved_avatar", None)
    if stashed is not None:
        return stashed
    return avatars.load(session.avatar_id)


def for_session_offpath(session) -> Avatar:
    """Background-task variant (self-introduction, finalize): may resolve and
    fill the stash when dispatch predates the feature or the instance
    restarted. Off the transcript hot path only."""
    stashed = getattr(session, "resolved_avatar", None)
    if stashed is not None:
        return stashed
    resolved = resolve_for_dispatch(
        getattr(session, "org_id", "") or "", session.avatar_id
    )
    try:
        session.resolved_avatar = resolved
    except Exception:  # noqa: BLE001 — a stash failure only costs a re-resolve
        pass
    return resolved


def _reset_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()
