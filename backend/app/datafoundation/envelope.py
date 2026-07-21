"""SourceEnvelope + typed ACL validation (accepted contract v5). Pure.

FAIL-CLOSED is the design center: a missing or unknown ``acl_mode`` NEVER
means org-readable; validation normalizes it to ``unknown`` and the DAL
stores such records with zero ACL rows (visible to nobody, absent from every
live index). ``upload`` must say ``org_default`` explicitly.
"""
from __future__ import annotations

import hashlib
from typing import Any

ENVELOPE_KINDS = ("document", "message", "event", "record")
ACL_MODES = ("org_default", "mirrored", "unknown")
ACCESS_LEVELS = ("reader", "writer", "owner")


def body_checksum(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def validate_envelope(payload: Any) -> tuple[dict, list[str]]:
    """(clean, errors). Errors mean QUARANTINE, never partial application."""
    if not isinstance(payload, dict):
        return {}, ["envelope must be an object"]
    errors: list[str] = []
    external_id = str(payload.get("external_id") or "").strip()[:300]
    if not external_id:
        errors.append("external_id is required")
    kind = str(payload.get("kind") or "")
    if kind.startswith("__body_"):
        # Sentinel from sync.materialize_bodies: quarantine with the exact
        # contract reason (e.g. body_pipeline_disabled), not a shape error.
        errors.append(kind.strip("_"))
    elif kind not in ENVELOPE_KINDS:
        errors.append(f"kind must be one of {list(ENVELOPE_KINDS)}")
    if not isinstance(payload.get("deleted"), bool):
        errors.append("deleted must be a boolean")
    acl_mode = payload.get("acl_mode")
    if acl_mode not in ACL_MODES:
        # Fail-closed normalization: an absent/invalid mode is 'unknown',
        # and we record the anomaly so connectors get fixed.
        acl_mode = "unknown"
    acl_raw = payload.get("acl")
    acl: list[dict] = []
    if acl_mode == "mirrored":
        if not isinstance(acl_raw, list) or not acl_raw:
            errors.append("acl entries are required when acl_mode=mirrored")
        else:
            for entry in acl_raw[:200]:
                if not isinstance(entry, dict):
                    errors.append("acl entries must be objects")
                    break
                p_kind = str(entry.get("principal_kind") or "")
                ext = str(entry.get("principal_external_id") or "").strip()
                access = str(entry.get("access") or "reader")
                if p_kind not in ("user", "group") or not ext:
                    errors.append("acl entry needs principal_kind user|group "
                                  "and principal_external_id")
                    break
                acl.append({
                    "principal_kind": p_kind,
                    "principal_external_id": ext[:300],
                    "access": access if access in ACCESS_LEVELS else "reader",
                })
    body_text = payload.get("body_text")
    if body_text is not None and not isinstance(body_text, str):
        errors.append("body_text must be a string")
        body_text = None
    checksum = str(payload.get("checksum") or "").strip()[:80]
    if not checksum:
        checksum = body_checksum(body_text or "")
    clean = {
        "external_id": external_id,
        "kind": kind if kind in ENVELOPE_KINDS else "document",
        "title": str(payload.get("title") or "")[:300],
        "body_text": body_text,
        "mime": str(payload.get("mime") or "")[:100],
        "canonical_url": str(payload.get("canonical_url") or "")[:500],
        "author_external_id": str(
            payload.get("author_external_id") or "")[:200],
        "container_external_id": str(
            payload.get("container_external_id") or "")[:300],
        "external_updated_at": str(
            payload.get("external_updated_at") or "")[:40],
        "acl_mode": acl_mode,
        "acl": acl,
        "deleted": bool(payload.get("deleted")),
        "checksum": checksum,
        "body_ref": str(payload.get("body_ref") or "")[:200],
        "transform": str(payload.get("transform") or "df@1")[:60],
    }
    return clean, errors
