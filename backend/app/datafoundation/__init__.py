"""Company Data Foundation (DF0-DF1) — accepted contract v5 + clarifications.

Connectors emit SourceEnvelopes; normalized records live as stable heads with
immutable versions; identities/ACLs are fail-closed mirrors; the
ContextResolver is the one retrieval boundary. Laura is the sole owner.

enabled() couples ONLY the flag and the control plane (binding: structured,
no-body records must work without the Company Brain). Body-bearing envelopes
reuse the knowledge storage/chunk pipeline AS A LIBRARY — when that flag is
off they quarantine with reason ``body_pipeline_disabled`` instead of
failing the run.
"""
from __future__ import annotations

from .. import control_plane
from ..config import settings


def enabled() -> bool:
    return bool(settings.data_foundation_enabled) and control_plane.enabled()
