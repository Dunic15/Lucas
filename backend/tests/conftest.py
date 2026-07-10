"""Shared test fixtures for the backend suite.

The suite's contract is KEY-FREE: every test must behave identically on a
clean checkout (no ``.env``) and on a developer machine whose repo-root
``.env`` carries real vendor keys — ``config.Settings`` loads that ``.env``
at import time. The autouse fixtures below enforce that contract per test:

* ``_keyfree_settings`` pins every settings field back to its code default.
  Without it, a keyed ``.env`` silently flips provider routing (stub →
  groq/anthropic, Recall "ready") and tests start making real, paid,
  rate-limited network calls whose outcomes depend on which tests ran
  before — the classic "passes alone, fails in combination" pollution.
* ``_reset_process_globals`` clears process-global singletons/registries
  that otherwise leak between tests: the Groq circuit breaker, the cached
  Anthropic client (created once with whatever key was live at FIRST use,
  then reused forever), ``main._finalizing`` (a bot_id stuck there turns
  every later ``/end`` for that id into a 202), and ``store._sessions``
  (in-memory rows left behind by a test that failed before its cleanup).

Tests that need a specific setting keep working unchanged: their own
``monkeypatch.setattr(settings, ...)`` runs after the autouse baseline and
wins; teardown restores everything in reverse order.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import llm
from app.config import Settings, settings


@pytest.fixture(autouse=True)
def _keyfree_settings(monkeypatch):
    """Pin every setting to its code default for the duration of each test."""
    for name, field in Settings.model_fields.items():
        monkeypatch.setattr(settings, name, field.default)


def _clear_registries() -> None:
    """Best-effort wipe of in-memory registries, resolved via sys.modules so
    importing conftest never forces app.main (FastAPI app) or app.store
    (sqlite init) into pure-unit test runs."""
    main = sys.modules.get("app.main")
    if main is not None:
        main._finalizing.clear()
    store = sys.modules.get("app.store")
    if store is not None:
        store._sessions.clear()


@pytest.fixture(autouse=True)
def _reset_process_globals():
    """Reset process-global LLM state and registries around every test, so a
    tripped Groq breaker, a stale cached Anthropic client, or a leftover
    session/finalize entry in one test can't change behavior in the next."""
    llm._reset_groq_breaker()
    llm._anthropic_client = None
    _clear_registries()
    yield
    llm._reset_groq_breaker()
    llm._anthropic_client = None
