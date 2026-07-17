"""Browser operator (B0) — a Laura avatar operating a watched remote browser.

B0 proves the contracts, security boundaries and feasibility with a
deterministic FAKE provider (zero network, zero keys). The provider is
replaceable behind the ``BrowserProvider`` interface; the smallest real
Browserbase adapter is optional and flag-gated OFF. Everything is inert
unless ``BROWSER_OPERATOR_ENABLED`` and the control plane are on — the
key-free demo never touches any of it.

Guarded WRITE steps are canonical actions (route='browser') on the existing
Action Control Plane — no second execution system.
"""
from __future__ import annotations

from .. import control_plane
from ..config import settings


def enabled() -> bool:
    return bool(settings.browser_operator_enabled) and control_plane.enabled()
