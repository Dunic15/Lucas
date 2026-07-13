"""Avatar registry.

An avatar is a folder under `avatars/<id>/` containing:
  - avatar.yaml   (the editable config)
  - knowledge/    (markdown process docs)

This module turns that folder into an `Avatar` object the rest of the code
uses. Adding an avatar = adding a folder. No code changes. See avatars/README.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import settings

_log = logging.getLogger(__name__)


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
    # Pose/gesture set for the /talk renderer ("F" | "M") — appended to the bot
    # page URL as ?body=; TalkingHead picks its masculine vs feminine idle set.
    # Defaulted so existing avatars (and tests building Avatar directly) are
    # untouched; the loader normalizes whatever avatar.yaml says.
    talk_body: str = "F"
    # Google Drive folder this avatar reads at session start (drive_client):
    # its docs become part of the pre-meeting brief. "" = no folder.
    drive_folder_id: str = ""
    # Silent notetaker mode: the avatar joins, listens, tracks the whole meeting
    # and builds/delivers the artifact at the end — but NEVER speaks during the
    # call (no greeting, answers, interventions, or nudges). For "just take
    # notes and hand off to Slack" rather than a talking participant.
    silent: bool = False
    # Which face this avatar wears in meetings — the product's two tiers:
    #   "talk"      -> free 3D model (TalkingHead, renders in the bot browser)
    #   "photoreal" -> Ultra-HD photoreal face (Ditto on the GPU box)
    #   ""          -> follow the global default (settings.avatar_page)
    # Per-avatar so the dashboard can flip a single avatar's tier by writing
    # this one field (avatar.yaml is mtime-cached: picked up with no restart).
    face: str = ""

    @property
    def page(self) -> str:
        """The renderer page this avatar's bot camera shows — its own `face`
        tier when set, else the global default. Values match the route names
        ("talk" | "photoreal" | "avatar")."""
        return self.face or settings.avatar_page

    @property
    def knowledge_dir(self) -> Path:
        return self.dir / "knowledge"

    @property
    def about_dir(self) -> Path:
        """Meta docs about the avatar ITSELF (architecture, playbook, costs).
        Indexed separately and retrieved only for self-questions ("how do you
        work?") — they must never pollute real process retrieval."""
        return self.dir / "about"

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

    @property
    def about_index_path(self) -> Path:
        return self.dir / ".about-index.json"


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
        talk_body=(
            "M"
            if str(_coalesce(raw.get("talk_body"), "F")).strip().upper().startswith("M")
            else "F"
        ),
        drive_folder_id=str(_coalesce(raw.get("drive_folder_id"), "")).strip(),
        silent=bool(raw.get("silent", False)),
        # face tier: only the known page names pass; anything else falls back
        # to "" (= global default) rather than producing a 404 camera URL.
        face=(lambda f: f if f in ("talk", "photoreal", "avatar") else "")(
            str(_coalesce(raw.get("face"), "")).strip().lower()
        ),
        dir=folder,
        knowledge_packs=[str(k) for k in (raw.get("knowledge_packs") or [])],
    )
    _load_cache[str(cfg_path)] = (mtime, avatar)
    return avatar


def is_internal(avatar_id: str) -> bool:
    """Whether this id is an INTERNAL persona (settings.internal_avatar_ids):
    hidden from every roster and refused by session dispatch for every caller.
    The folder may still exist (its removal is a separate track) and load()
    still works for direct/legacy uses — this only gates listing + dispatch.
    Reviewed decision (2026-07-13): /demo/ask can still load an internal
    avatar by explicit id — the guard is dispatch-scoped (per-minute meter +
    meeting presence), not knowledge-access control; folder removal (Codex's
    track) closes the rest."""
    return (avatar_id or "").strip().lower() in settings.internal_avatar_id_set


CUSTOMER_AVATAR_IDS = frozenset({"laura", "cedric"})


def is_customer_enabled(avatar_id: str) -> bool:
    """Whether an avatar may be named by an end-user or public product route.

    Internal/test personas and knowledge-pack folders can remain on disk for
    explicit offline administration, but are never customer-callable.
    """
    normalized = (avatar_id or "").strip().lower()
    return (
        normalized in CUSTOMER_AVATAR_IDS
        and not is_internal(normalized)
        and (settings.avatars_dir / normalized / "avatar.yaml").is_file()
    )


def list_ids() -> list[str]:
    """Installed, LISTABLE avatar folders. Internal personas
    (settings.internal_avatar_ids) are excluded here — the single choke point
    all rosters flow through (/avatars, dashboard, org grants, invite-tag
    routing) — so an internal folder can never be enumerated or summoned by
    tag even while it exists on disk. load() is deliberately NOT filtered."""
    root = settings.avatars_dir
    if not root.exists():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if (p / "avatar.yaml").exists() and not is_internal(p.name)
    )


def list_for_org(org_id: str) -> list[str]:
    """The avatar ids a member of ``org_id`` may call, sorted.

    An org with explicit grants (org_agents rows) sees ONLY those, intersected
    with the avatars that still exist as folders — a revoked/renamed folder
    never yields a dead entry. An org with NO grants (personal orgs, the Demo
    org, an unknown org) sees EVERY installed avatar: today's behavior, kept
    backward-compatible for the key-free demo and personal-org logins.
    """
    from . import store  # lazy: neither module imports the other at load time

    granted = store.list_org_agent_ids(org_id)
    if not granted:
        return list_ids()
    existing = set(list_ids())
    resolved = sorted(a for a in granted if a in existing)
    if not resolved:
        # Grants exist but every one references a missing folder (e.g. a folder
        # was renamed/removed without updating org_agents). Fail OPEN to all
        # avatars rather than handing the org a dead, empty dashboard. org_id is
        # a synthetic id + a count — no PII/transcript in this log.
        _log.warning(
            "org %s has %d agent grant(s) but none resolve to a folder; "
            "falling back to all avatars",
            org_id,
            len(granted),
        )
        return list_ids()
    return resolved


def list_customer_ids(org_id: str = "") -> list[str]:
    """Customer-callable roster, optionally intersected with an org's grants."""
    roster = list_for_org(org_id) if org_id else list_ids()
    return [avatar_id for avatar_id in roster if is_customer_enabled(avatar_id)]


# ── every avatar gets an email address, for free ──────────────────────
# The platform watches ONE inbox (the calendar/Gmail account). Gmail plus-
# aliases make that inbox an address PER AVATAR with zero extra accounts:
# inviting  laura.ai.122222+cedric@gmail.com  is "Cedric's email" — same
# inbox, and the +tag names the avatar that should join. The bare address
# (or an unknown tag) stays the default avatar.

def email_parts(address: str) -> tuple[str, str, str]:
    """lowercase (local-without-tag, tag, domain) of an email address."""
    addr = (address or "").strip().lower()
    local, _, domain = addr.partition("@")
    base, _, tag = local.partition("+")
    return base, tag, domain


def from_invite_email(addresses: "Iterable[str]", bases: "Iterable[str]") -> str | None:
    """The avatar id named by a plus-tagged invite address, or None.

    `addresses` are the invite/recipient emails seen on the event or message;
    `bases` the configured inbox address(es). Only a tag that matches an
    installed avatar id counts — anything else falls back to the caller's
    default, so a typo'd tag can never summon a ghost."""
    known = set(list_customer_ids())
    base_keys = set()
    for b in bases:
        if b:
            base, _tag, domain = email_parts(b)
            base_keys.add((base, domain))
    for address in addresses or ():
        base, tag, domain = email_parts(address)
        if tag and (base, domain) in base_keys and tag in known:
            return tag
    return None
