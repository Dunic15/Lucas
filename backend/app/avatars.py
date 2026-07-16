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
    # Identity-safe renderer chain. Asset filenames are explicit in avatar.yaml;
    # a missing asset is surfaced as unavailable, never borrowed from an avatar.
    face_fallback: str = "talk"
    talk_model: str = ""
    photoreal_reference: str = ""
    # Optional standing MISSION for this avatar's meetings — an objective she
    # keeps in mind and RESURFACES if left unmet ("on an investor call, if they
    # haven't covered market size, raise it"). A per-session mission
    # (MeetingContext.mission) overrides this default. "" = no mission = today's
    # behaviour exactly. Folded into the live + closing prompts as an instruction,
    # never a gate: turn-taking / hand-raise still decide WHEN she may speak, so
    # she never barges in.
    mission: str = ""
    # Optional DETERMINISTIC TASK HINTS: recognizable asks this avatar can act on,
    # each a {name, triggers[], action} dict. They BIAS post-meeting action
    # capture so an agreed, recognized ask is extracted as that typed action
    # instead of pure LLM improv. Finalize-only (never on the live path); the
    # transcript-evidence filter still applies, so a hint can never fabricate an
    # action the transcript doesn't support. [] = no hints = unchanged.
    tasks: list[dict] = None  # type: ignore[assignment]
    # Avatar-specific NATIVE tools this avatar is purpose-built for, declared in
    # avatar.yaml (e.g. Petra -> ["asana"]). These default ON for THIS avatar
    # only — every other avatar defaults them OFF even when the org has
    # connected them (a per-avatar dashboard toggle can still override). Google
    # Calendar + Gmail are NOT listed here: they are BASELINE for every avatar.
    # [] = only the baseline tools.
    native_tools: list[str] = None  # type: ignore[assignment]

    def uses_native_tool(self, name: str) -> bool:
        """Whether this avatar is purpose-built for a gated native tool."""
        return str(name).strip().lower() in (self.native_tools or [])

    @property
    def page(self) -> str:
        """The renderer page this avatar's bot camera shows — its own `face`
        tier when set, else the global default. Values match the route names
        ("talk" | "photoreal" | "avatar")."""
        return self.face or settings.avatar_page

    @property
    def renderer_readiness(self) -> dict:
        """Configured face assets and their on-disk readiness, safe for the API."""
        repo = self.dir.parent.parent
        talk_name = self.talk_model or f"{self.id}.glb"
        portrait_name = self.photoreal_reference or f"reference-{self.id}.jpg"
        talk_safe = Path(talk_name).name == talk_name
        portrait_safe = Path(portrait_name).name == portrait_name
        talk_ready = talk_safe and (repo / "frontend" / talk_name).is_file()
        photoreal_ready = (
            portrait_safe and (repo / "gpu" / "assets" / portrait_name).is_file()
        )
        preferred_ready = (
            photoreal_ready if self.page == "photoreal"
            else talk_ready if self.page == "talk"
            else bool(self.anam_avatar_id)
        )
        return {
            "preferred": self.page,
            "fallback": self.face_fallback,
            "ready": preferred_ready,
            "talk": {"ready": talk_ready, "asset": talk_name},
            "photoreal": {"ready": photoreal_ready, "asset": portrait_name},
        }

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


def _normalize_tasks(raw_tasks) -> list[dict]:
    """avatar.yaml ``tasks`` -> a clean list of ``{name, triggers[], action}``.

    Deterministic task hints: recognizable asks this avatar can act on. Each
    entry is a dict with a ``name``, one or more ``triggers`` (phrases that signal
    the ask), and the ``action`` intent to record. Malformed or trigger-less
    entries are dropped so a typo can never inject a blank hint; a missing/blank
    ``tasks`` yields ``[]`` (behaviour identical to an avatar with no tasks)."""
    out: list[dict] = []
    for entry in raw_tasks or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        action = str(entry.get("action") or "").strip()
        raw_triggers = entry.get("triggers") or entry.get("trigger") or []
        if isinstance(raw_triggers, str):
            raw_triggers = [raw_triggers]
        triggers = [str(t).strip() for t in raw_triggers if str(t).strip()]
        # A hint needs something to recognize (triggers) and something to record
        # (an action or at least a name) — otherwise it can't bias anything.
        if not triggers or not (name or action):
            continue
        out.append(
            {"name": name or action, "triggers": triggers, "action": action or name}
        )
    return out


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
        face_fallback=(
            lambda f: f if f in ("talk", "photoreal", "avatar", "none") else "talk"
        )(str(_coalesce(raw.get("face_fallback"), "talk")).strip().lower()),
        talk_model=str(_coalesce(raw.get("talk_model"), f"{avatar_id}.glb")).strip(),
        photoreal_reference=str(
            _coalesce(raw.get("photoreal_reference"), f"reference-{avatar_id}.jpg")
        ).strip(),
        mission=(raw.get("mission") or "").strip(),
        tasks=_normalize_tasks(raw.get("tasks")),
        native_tools=[
            str(t).strip().lower() for t in (raw.get("native_tools") or []) if str(t).strip()
        ],
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
    known = set(list_ids())
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
