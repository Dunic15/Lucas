"""Shared test fixtures for the backend suite.

The suite's contract is KEY-FREE: every test must behave identically on a
clean checkout (no ``.env``) and on a developer machine whose repo-root
``.env`` carries real vendor keys. ``config.Settings`` loads that ``.env``
at import time. The autouse fixtures below enforce that contract per test:

* ``_keyfree_settings`` pins every settings field back to its code default.
  Without it, a keyed ``.env`` silently flips provider routing (stub →
  groq/anthropic, Recall "ready") and tests start making real, paid,
  rate-limited network calls whose outcomes depend on which tests ran
  before; the classic "passes alone, fails in combination" pollution.
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

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import avatars, llm
from app.config import Settings, settings


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "pg: control-plane tests against embedded Postgres (pgserver); "
        "auto-skipped when pgserver isn't installed",
    )


@pytest.fixture(autouse=True)
def _keyfree_settings(monkeypatch):
    """Pin every setting to its code default for the duration of each test."""
    for name, field in Settings.model_fields.items():
        monkeypatch.setattr(settings, name, field.default)


@pytest.fixture(autouse=True)
def _wake_word_inherits_global(monkeypatch):
    """The shipped avatar yamls set ``require_wake_word: true``. Most of the
    behavior suite predates that and exercises machinery (nudges, greetings,
    hand-raise, proactive interventions) only reachable with wake mode off -
    so loaded avatars are pinned back to "inherit the global default", which
    ``_keyfree_settings`` keeps off. The shipped-yaml state itself is covered
    by test_wake_word_per_avatar.py, which overrides this fixture."""
    real_load = avatars.load
    # Memoize per avatar so repeated loads return the SAME instance; the
    # normalized copy must still honour avatars.load's identity contract
    # (avatars.load(x) is avatars.load(x)), which the org-avatar-overlay
    # resolution tests assert. Re-derive only when the underlying cached
    # instance changes (an mtime refresh).
    _memo: dict[str, tuple[avatars.Avatar, avatars.Avatar]] = {}

    def load(avatar_id: str) -> avatars.Avatar:
        # Copy, never mutate: real_load returns a shared cached instance.
        base = real_load(avatar_id)
        cached = _memo.get(avatar_id)
        if cached is None or cached[0] is not base:
            replaced = dataclasses.replace(base, require_wake_word=None)
            _memo[avatar_id] = (base, replaced)
            return replaced
        return cached[1]

    monkeypatch.setattr(avatars, "load", load)


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
    google_client = sys.modules.get("app.google_client")
    if google_client is not None:
        google_client._reset_token_cache()
    asana_client = sys.modules.get("app.asana_client")
    if asana_client is not None:
        asana_client._reset_brief_cache()
    graphiti_client = sys.modules.get("app.graphiti_client")
    if graphiti_client is not None:
        graphiti_client.reset_for_tests()
    jira_client = sys.modules.get("app.jira_client")
    if jira_client is not None:
        jira_client._reset_brief_cache()


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
