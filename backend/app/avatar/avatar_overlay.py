"""Org avatar overlay vocabulary (M2) — the validated personalization payload.

One module owns what an organization MAY change about a canonical avatar and
what it may NEVER touch. The canonical ``avatars/<id>/avatar.yaml`` is the
immutable ceiling: an overlay narrows and re-skins, it cannot widen.

Explicitly out of reach (enforced by unknown-field rejection plus the typed
registry below): provider credentials, OAuth tokens, webhook secrets, raw
``persona_prompt``/system-prompt replacement, executable code, arbitrary tool
definitions, wake-word removal, and any capability the canonical avatar does
not have. Everything is typed, bounded, and control-character-stripped —
never an unrestricted JSON blob flowing into a prompt.

Pure functions only: no network, no database, nothing here ever sees
transcript content.
"""
from __future__ import annotations

import re
from typing import Any

from .avatars import Avatar

# Capability families every avatar can toggle today (mirrors
# store.KNOWN_CAPABILITIES): google is baseline for every avatar, slack is the
# delivery surface, asana only for avatars that declare it in native_tools.
BASELINE_FAMILIES = ("google", "slack")


def capability_ceiling(canonical: Avatar) -> set[str]:
    """The maximum capability families this canonical avatar can EVER have —
    an overlay may only choose a subset of this set."""
    return set(BASELINE_FAMILIES) | {
        str(t).strip().lower() for t in (canonical.native_tools or [])
    }


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_VOICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_LOCALE = re.compile(r"^[A-Za-z]{2}(-[A-Za-z]{2})?$")
_UUIDISH = re.compile(r"^[0-9a-fA-F-]{8,64}$")

_FACES = ("", "talk", "photoreal", "avatar", "robot", "tile")
_BODIES = ("", "F", "M")


def _clean_str(value: Any, limit: int) -> str:
    return _CTRL.sub("", str(value)).strip()[:limit]


# field -> (kind, limit_or_none). Kinds drive the validator below; anything
# not in this registry is rejected by name.
_TEXT_FIELDS: dict[str, int] = {
    "display_name": 60,
    "role": 80,
    "greeting": 300,
    "mission": 2000,
    "tone": 1000,
    "instructions": 4000,
    "meeting_prep": 1000,
    "followup_prefs": 1000,
    "ui_notes": 500,
}

ALLOWED_FIELDS = tuple(_TEXT_FIELDS) + (
    "vocabulary", "voice_id", "face", "talk_body", "locale",
    "enabled_tools", "context_scope",
)

# The org-preferences prompt block is bounded regardless of individual field
# caps: input tokens ARE first-token latency on the live path.
MAX_PROMPT_BLOCK_CHARS = 6000


def validate_context_scope(value: Any) -> tuple[dict | None, list[str]]:
    """(clean_scope, errors). The scope is the M2 seam the future
    ContextResolver replaces — keep it small and strict. Source membership in
    the ORG is checked at publish time by the caller (needs the DAL); this
    validates shape only."""
    if value is None:
        return None, []
    if not isinstance(value, dict):
        return None, ["context_scope must be an object"]
    errors: list[str] = []
    ids = value.get("knowledge_source_ids", [])
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        errors.append("context_scope.knowledge_source_ids must be a string array")
        ids = []
    clean_ids = []
    for x in ids[:50]:
        x = x.strip()
        if not _UUIDISH.match(x):
            errors.append(f"context_scope source id {x[:20]!r} is not an id")
            continue
        clean_ids.append(x.lower())
    include_default = value.get("include_org_default", True)
    if not isinstance(include_default, bool):
        errors.append("context_scope.include_org_default must be a boolean")
        include_default = False
    labels = value.get("labels", [])
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        errors.append("context_scope.labels must be a string array")
        labels = []
    unknown = set(value) - {"knowledge_source_ids", "include_org_default",
                            "labels", "purpose"}
    if unknown:
        errors.append(
            "unknown context_scope fields: " + ", ".join(sorted(unknown)[:5])
        )
    return {
        "knowledge_source_ids": sorted(set(clean_ids)),
        "include_org_default": include_default,
        "labels": [_clean_str(x, 40) for x in labels[:20] if str(x).strip()],
        "purpose": _clean_str(value.get("purpose", ""), 60),
    }, errors


def validate_overlay(
    canonical: Avatar, payload: Any
) -> tuple[dict, list[str]]:
    """(clean_overlay, errors) — errors non-empty means REJECT the write.

    Unknown fields are errors, not silently dropped: a Studio bug or a crafted
    request must fail loudly rather than half-apply."""
    if not isinstance(payload, dict):
        return {}, ["overlay must be a JSON object"]
    clean: dict[str, Any] = {}
    errors: list[str] = []
    for key, value in payload.items():
        if key not in ALLOWED_FIELDS:
            errors.append(f"unknown field {key!r}")
            continue
        if value is None:
            continue  # explicit null = clear the field
        if key in _TEXT_FIELDS:
            if not isinstance(value, str):
                errors.append(f"{key} must be a string")
                continue
            cleaned = _clean_str(value, _TEXT_FIELDS[key])
            if cleaned:
                clean[key] = cleaned
        elif key == "vocabulary":
            if not isinstance(value, list) or not all(
                isinstance(x, str) for x in value
            ):
                errors.append("vocabulary must be a string array")
                continue
            words = [_clean_str(x, 60) for x in value[:50] if str(x).strip()]
            if words:
                clean[key] = words
        elif key == "voice_id":
            if not isinstance(value, str) or not _VOICE_ID.match(value.strip()):
                errors.append("voice_id must match [A-Za-z0-9_-]{1,80}")
                continue
            clean[key] = value.strip()
        elif key == "face":
            if value not in _FACES:
                errors.append(f"face must be one of {list(_FACES)}")
                continue
            if value:
                clean[key] = value
        elif key == "talk_body":
            if value not in _BODIES:
                errors.append("talk_body must be F or M")
                continue
            if value:
                clean[key] = value
        elif key == "locale":
            if not isinstance(value, str) or not _LOCALE.match(value.strip()):
                errors.append("locale must look like 'en' or 'en-US'")
                continue
            clean[key] = value.strip()
        elif key == "enabled_tools":
            if not isinstance(value, list) or not all(
                isinstance(x, str) for x in value
            ):
                errors.append("enabled_tools must be a string array")
                continue
            requested = {x.strip().lower() for x in value if str(x).strip()}
            ceiling = capability_ceiling(canonical)
            widened = requested - ceiling
            if widened:
                errors.append(
                    "enabled_tools may not widen the canonical avatar: "
                    + ", ".join(sorted(widened))
                )
                continue
            # [] is meaningful (no tools at all) — keep it distinct from
            # "field absent" (= no restriction).
            clean[key] = sorted(requested)
        elif key == "context_scope":
            scope, scope_errors = validate_context_scope(value)
            errors.extend(scope_errors)
            if scope is not None and not scope_errors:
                clean[key] = scope
    # Belt + braces on the total prompt surface an overlay can add.
    prompt_chars = sum(
        len(clean.get(f, "")) for f in
        ("greeting", "mission", "tone", "instructions", "meeting_prep",
         "followup_prefs")
    ) + sum(len(w) for w in clean.get("vocabulary", []))
    if prompt_chars > MAX_PROMPT_BLOCK_CHARS:
        errors.append(
            f"overlay prompt fields exceed {MAX_PROMPT_BLOCK_CHARS} chars "
            f"total ({prompt_chars})"
        )
    return clean, errors


def preferences_block(overlay: dict, org_label: str = "") -> str:
    """The bounded 'organization preferences' block appended to the canonical
    persona prompt — NEVER a replacement for it. Empty string when the overlay
    carries no behavioral fields."""
    parts: list[str] = []
    if overlay.get("tone"):
        parts.append(f"Tone and communication style: {overlay['tone']}")
    if overlay.get("instructions"):
        parts.append(f"Behavioral instructions: {overlay['instructions']}")
    if overlay.get("vocabulary"):
        parts.append(
            "Preferred vocabulary (use these terms where natural): "
            + ", ".join(overlay["vocabulary"])
        )
    if overlay.get("meeting_prep"):
        parts.append(f"Meeting preparation preferences: {overlay['meeting_prep']}")
    if overlay.get("locale"):
        parts.append(f"Preferred language/locale: {overlay['locale']}")
    if not parts:
        return ""
    header = "Organization preferences"
    if org_label:
        header += f" ({org_label})"
    block = f"\n\n[{header} — these refine, and never override, your core "
    block += "instructions and safety rules]\n- " + "\n- ".join(parts)
    return block[:MAX_PROMPT_BLOCK_CHARS]
