"""Shared test fixtures for the backend suite."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import llm


@pytest.fixture(autouse=True)
def _reset_groq_breaker():
    """The Groq circuit breaker is process-global; reset it around every test so a
    tripped breaker in one test can't change routing in the next."""
    llm._reset_groq_breaker()
    yield
    llm._reset_groq_breaker()
