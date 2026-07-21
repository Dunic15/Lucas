"""Per-integration skills — lazily-loaded guidance for ONE app at a time.

The pattern (mirrors Cedric's integrationSkills): most integrations work out of
the box through Pipedream and need NO skill. When one misbehaves, drop a short
markdown playbook at ``backend/skills/integrations/<app_slug>.md`` describing
how to navigate that app well (typing rules, API quirks, required headers).
It is read ONLY when the agent is actually working with that app — never in
the base prompt — so the token cost is zero for meetings that don't touch it.

Consumers today:
  * brain.engine.type_actions — appends the app's skill to the typing prompt
    when the avatar may use that app (e.g. Asana for Petra).
Future: the pd_read/brain tool surface loads the skill on first use of an app
in a turn, exactly like Cedric injects playbooks into the first tool result.

Files are tiny and cached (mtime-checked) — a hot edit lands without restart.
No skill file ⇒ empty string, callers add nothing to their prompts.
"""
from __future__ import annotations

import threading
from pathlib import Path

# backend/skills/integrations/<slug>.md  (backend/ is this file's grandparent)
_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills" / "integrations"
_MAX_CHARS = 4000  # a skill is a playbook, not a manual — hard cap for prompts

_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}  # slug -> (mtime, body)


def _safe_slug(app_slug: str) -> str:
    slug = (app_slug or "").strip().lower()
    # File-name safety: Pipedream slugs are [a-z0-9_-]; anything else is not a
    # skill lookup, it's garbage input.
    return slug if slug and all(c.isalnum() or c in "_-" for c in slug) else ""


def skill_for(app_slug: str) -> str:
    """The markdown playbook for one app, or '' when none exists (the normal
    case — most apps need no skill). Cached, mtime-invalidated, size-capped.
    Never raises."""
    slug = _safe_slug(app_slug)
    if not slug:
        return ""
    path = _SKILLS_DIR / f"{slug}.md"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    with _lock:
        hit = _cache.get(slug)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        body = path.read_text(encoding="utf-8").strip()[:_MAX_CHARS]
    except OSError:
        return ""
    with _lock:
        _cache[slug] = (mtime, body)
    return body


def _reset() -> None:
    """Test seam."""
    with _lock:
        _cache.clear()
