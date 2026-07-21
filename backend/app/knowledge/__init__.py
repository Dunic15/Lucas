"""Durable Company Brain (M1); org-owned knowledge behind one flag.

The retrieval seam predates this package (rag.retrieve(org_id=...) merges a
per-(org, avatar) index file into the base pack); what lives here is the
INGESTION side that was never built: durable sources/documents/chunks on the
FORCE-RLS control plane, a claim/lease ingest worker, and the bridge that
rebuilds the in-memory index files FROM Postgres so they survive deploys.

Everything is inert unless COMPANY_BRAIN_ENABLED=true AND the control plane
is configured; the key-free demo never touches any of it.
"""
from __future__ import annotations

from .. import control_plane
from ..config import settings


def enabled() -> bool:
    """The one switch every entry point checks."""
    return bool(settings.company_brain_enabled) and control_plane.enabled()
