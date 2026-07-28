"""Tenant-scoped gates for the OpenClaw executor experiment."""
from __future__ import annotations

import re

from ..config import settings


def allowlisted_orgs() -> set[str]:
    """Org ids explicitly allowed to use the experiment."""
    raw = settings.openclaw_experiment_orgs or ""
    return {part.strip() for part in re.split(r"[\s,]+", raw) if part.strip()}


def experiment_enabled_for_org(org_id: str | None) -> bool:
    """True only when the global switch and tenant allowlist both match."""
    org = str(org_id or "").strip()
    return bool(settings.openclaw_experiment_enabled and org in allowlisted_orgs())


def snapshot(org_id: str | None) -> dict:
    """Small, safe status payload for the dashboard."""
    org = str(org_id or "").strip()
    return {
        "enabled": bool(settings.openclaw_experiment_enabled),
        "allowlisted": bool(org and org in allowlisted_orgs()),
        "active": experiment_enabled_for_org(org),
        "auto_run": bool(settings.openclaw_auto_run),
        "gateway_configured": bool((settings.openclaw_gateway_url or "").strip()),
        "browser_enabled": bool(settings.openclaw_browser_enabled),
        "agent_id": settings.openclaw_agent_id or "laura-executor-test",
    }
