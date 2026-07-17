"""Northstar MVP demo integration (final integration branch).

The glue that wires the completed pieces into ONE testable Laura MVP:
Browser B1 (perception + coordinator + canonical approval) drives the Northstar
synthetic product, ContextResolver grounds the narration, and the guarded
follow-up task flows through the existing Action Control Plane. This package
ADDS glue only — it reuses every canonical seam and creates no second action /
RAG / browser / planner / demo system.

Everything here is inert unless ``NORTHSTAR_DEMO_ENABLED`` is on AND the browser
operator is enabled (which itself needs the control plane). With the flag off,
the demo router 404s, the ``northstar`` provider is never selected, and no code
path here runs — production behaviour is byte-identical.
"""
from __future__ import annotations

from .. import browser
from ..config import settings


def enabled() -> bool:
    return bool(settings.northstar_demo_enabled) and browser.enabled()


def write_enabled() -> bool:
    """The ONE controlled synthetic write (create_followup_task). Arbitrary
    browser writes stay disabled; this narrow flag gates only the Northstar
    follow-up task."""
    return enabled() and bool(settings.northstar_demo_write_enabled)
