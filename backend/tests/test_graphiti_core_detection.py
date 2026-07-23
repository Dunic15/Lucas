"""graphiti-core lives in a sidecar; detection must look there.

Bug 2026-07-23: the Brain card showed "Blocked" and System Check showed
"graphiti-core not installed" after every deploy, even though the live probe
succeeded. Cause: graphiti-core is installed in the ``_graphiti_libs`` SIDECAR
(added to sys.path only lazily, on first Graphiti use), but the dashboard
checked presence at MODULE LOAD via importlib.metadata — before the sidecar was
on the path — and cached the empty result. ``core_installed()`` now adds the
sidecar to sys.path itself and reports honestly.
"""
from __future__ import annotations

import sys

import pytest

from app import config as cfg
from app.integrations import graphiti_client


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    graphiti_client._core_version = None
    before = list(sys.path)
    yield
    sys.path[:] = [p for p in sys.path if "_graphiti_libs" not in p] or before
    graphiti_client._core_version = None


def test_finds_graphiti_core_in_the_sidecar(tmp_path, monkeypatch):
    # Mirror the App Runner layout: REPO_ROOT/_graphiti_libs/graphiti_core/
    pkg = tmp_path / "_graphiti_libs" / "graphiti_core"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("__version__ = '9.9.9-test'\n")
    monkeypatch.setattr(cfg, "REPO_ROOT", tmp_path)

    assert graphiti_client.core_installed(), "sidecar package must be detected"


def test_absent_sidecar_reports_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "REPO_ROOT", tmp_path)  # no _graphiti_libs here
    assert graphiti_client.core_installed() == ""


def test_result_is_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "REPO_ROOT", tmp_path)
    assert graphiti_client.core_installed() == ""
    # Even if a sidecar appears later, the cached process value stands until a
    # redeploy — presence can't change without one.
    pkg = tmp_path / "_graphiti_libs" / "graphiti_core"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("__version__ = '1.0'\n")
    assert graphiti_client.core_installed() == ""  # still cached
