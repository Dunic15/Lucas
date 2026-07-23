"""graphiti-core detection must be sidecar-aware, not boot-time-metadata.

Bug 2026-07-23: the Brain card showed "Blocked" and System Check showed
"graphiti-core not installed" after a deploy, even though the live probe did a
full connect+ingest+recall successfully. graphiti-core is installed in the
``_graphiti_libs`` SIDECAR, on sys.path only lazily (first Graphiti use), but
the dashboard checked presence at MODULE LOAD via importlib.metadata — before
the sidecar was on the path — and cached the empty result. ``core_installed()``
adds the sidecar to sys.path itself and resolves the version robustly.

Whether graphiti-core is importable in THIS interpreter differs between a dev
venv and CI, so these tests mock the import surface to stay deterministic.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util

import pytest

from app.integrations import graphiti_client


@pytest.fixture(autouse=True)
def _reset_cache():
    graphiti_client._core_version = None
    yield
    graphiti_client._core_version = None


def test_reports_version_when_importable(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: object() if name == "graphiti_core" else None)
    monkeypatch.setattr(importlib.metadata, "version",
                        lambda pkg: "9.9.9" if pkg == "graphiti-core" else "")
    assert graphiti_client.core_installed() == "9.9.9"


def test_importable_without_metadata_still_reports_installed(monkeypatch):
    """The sidecar package may lack .dist-info — importable is enough."""
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())

    def _missing(pkg):
        raise importlib.metadata.PackageNotFoundError(pkg)

    monkeypatch.setattr(importlib.metadata, "version", _missing)
    assert graphiti_client.core_installed() == "installed"


def test_absent_reports_empty(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert graphiti_client.core_installed() == ""


def test_result_is_cached(monkeypatch):
    """Presence can't change without a redeploy, so probe once per process."""
    calls: list = []
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: calls.append(name) or None)
    assert graphiti_client.core_installed() == ""
    assert graphiti_client.core_installed() == ""
    assert len(calls) == 1, "find_spec must run once, then serve the cache"


def test_never_raises(monkeypatch):
    def _boom(name):
        raise RuntimeError("import machinery exploded")

    monkeypatch.setattr(importlib.util, "find_spec", _boom)
    assert graphiti_client.core_installed() == ""
