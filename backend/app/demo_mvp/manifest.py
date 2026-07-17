"""Loader for the Northstar demo manifest (the versioned demo definition).

The manifest is a STATIC, non-authoritative asset. It may provide the starting
URL, goal, checkpoints, expected visible results, allowed domains, budgets and
guarded-operation metadata — but it can NEVER override organization/principal,
browser policy, permission checks, action approval, the operation vocabulary,
or the global safety limits (those are clamped server-side).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

# demos/northstar/demo/demo_manifest.json — repo-root relative.
_MANIFEST = (Path(__file__).resolve().parents[3]
             / "demos" / "northstar" / "demo" / "demo_manifest.json")
_KNOWLEDGE = (Path(__file__).resolve().parents[3]
              / "demos" / "northstar" / "knowledge")


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads(_MANIFEST.read_text())


def knowledge_dir() -> Path:
    return _KNOWLEDGE


def demo_id() -> str:
    m = load()
    return f"{m['company_id']}:{m['demo_version']}"


def allowed_domains() -> str:
    """The manifest's allowed domains, as the comma-separated form the browser
    policy consumes. Server config remains the outer authority."""
    return ",".join(load().get("allowed_domains") or [])


def clamped_budgets() -> dict[str, int]:
    """Manifest budgets CLAMPED to the server maximums — a manifest can only
    tighten a limit, never widen it past the coordinator's global cap."""
    from ..config import settings

    m = load()
    return {
        "max_steps": min(int(m.get("max_steps") or settings.browser_coord_max_steps),
                         int(settings.browser_coord_max_steps)),
        "max_duration_seconds": min(
            int(m.get("max_duration_seconds")
                or settings.browser_coord_max_duration_seconds),
            int(settings.browser_coord_max_duration_seconds)),
    }
