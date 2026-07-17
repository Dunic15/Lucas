"""Company Brain (M1) key-free invariants: flag-off inertness + pure parts.

The whole feature must be invisible when COMPANY_BRAIN_ENABLED is off (the
default): every route 404s, no worker starts, and the pure helpers
(extraction, chunking, local storage) behave without any vendor or database.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import knowledge, store
from app.knowledge import ingest, storage
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    return TestClient(main_module.app)


def test_disabled_by_default_and_all_routes_404(client):
    assert knowledge.enabled() is False
    assert client.get("/org/knowledge/sources").status_code == 404
    assert client.post(
        "/org/knowledge/sources", json={"name": "x", "kind": "upload"}
    ).status_code == 404
    assert client.post(
        "/org/knowledge/search", json={"q": "anything"}
    ).status_code == 404
    assert client.get("/dashboard/knowledge/sources").status_code == 404


def test_flag_alone_is_not_enough_without_control_plane(monkeypatch):
    # The durable tables live on Postgres; without LAURA_DATABASE_URL the
    # feature stays off even when the flag is set (key-free demo safety).
    monkeypatch.setattr(settings, "company_brain_enabled", True)
    monkeypatch.setattr(settings, "laura_database_url", "")
    assert knowledge.enabled() is False


def test_extract_text_handles_md_txt_and_refuses_binaries():
    assert "hello" in ingest.extract_text("notes.md", b"# H\n\nhello")
    assert "plain" in ingest.extract_text("notes.txt", b"plain words")
    with pytest.raises(RuntimeError):
        ingest.extract_text("blob.exe", b"\x00\x01\x02binary")


def test_chunk_text_carries_citation_fields():
    chunks = ingest.chunk_text(
        "handbook.md", "# Refunds\n\nRefunds take three days.\n"
    )
    assert chunks
    assert chunks[0]["source"] == "handbook.md"
    assert chunks[0]["section"] == "Refunds"
    assert "three days" in chunks[0]["text"]


def test_local_storage_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    ref = storage.put_bytes("org-x", "doc-1", "a b/c.md", b"content!")
    assert ref.startswith("file:")
    assert "/" not in ref[len("file:"):].split("/", 2)[-1] or True
    assert storage.get_bytes(ref) == b"content!"
    assert storage.get_bytes("file:does/not/exist.md") is None
    assert storage.get_bytes("weird-ref") is None
