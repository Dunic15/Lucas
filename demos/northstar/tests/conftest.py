"""Test harness for the Northstar demo — network-free, deterministic.

Adds the repo root to sys.path so `demos.northstar...` imports resolve, drives
the product in-process via TestClient, and resets state before every test so
each starts from the identical frozen seed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEMO_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_state():
    from demos.northstar.product import store
    store.reset()
    yield
    store.reset()


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from demos.northstar.product.app import app
    return TestClient(app)


@pytest.fixture
def demo_dir() -> Path:
    return DEMO_DIR
