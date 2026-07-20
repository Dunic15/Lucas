"""Shared helpers used by more than one api/ router (and by main.py) — hoisted
here so route groups can be extracted into api/ without importing main.py, which
would be circular. Keep this small: only genuinely cross-cutting helpers."""
import re

from app.core.config import settings

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def _split_emails(raw: str) -> set[str]:
    return {e.lower() for e in EMAIL_RE.findall(raw or "")}


def _calendar_target_emails() -> set[str]:
    return _split_emails(settings.calendar_invite_emails)
