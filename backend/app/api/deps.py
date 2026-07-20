"""Shared helpers hoisted from main.py (email targeting, gmail-watch state,
bilingual line picker). Landing spot for cross-router helpers."""
import random
import re

from ..config import settings
from ..brain.engine import sounds_italian


EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def _split_emails(raw: str) -> set[str]:
    return {e.lower() for e in EMAIL_RE.findall(raw or "")}


def _calendar_target_emails() -> set[str]:
    return _split_emails(settings.calendar_invite_emails)


_gmail_state = {"last_poll": 0.0, "last_error": "", "joined": []}


def _line_for(heard: str, en: list, it: list) -> str:
    """A random line from the pool matching the language of what was heard."""
    return random.choice(it if sounds_italian(heard) else en)
