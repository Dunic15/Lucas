"""Upload size cap must reject BEFORE allocating the decoded buffer.

b64decode (and str.encode) allocate the full output up front, so a cap checked
only after decoding admits an arbitrary-size allocation first — an OOM hazard
on the 2 GB instance that also runs live meetings (review finding 2026-07-20).
Pure unit tests on the shared _upload_document implementation; no DB needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.knowledge import router as kroute


def test_oversize_base64_rejected_without_decoding(monkeypatch):
    def boom(*a, **k):  # decode must never run for an oversize payload
        raise AssertionError("b64decode ran before the size cap")

    monkeypatch.setattr(kroute.base64, "b64decode", boom)
    huge = "A" * (settings.knowledge_max_file_bytes * 4 // 3 + 9)
    status, body = kroute._upload_document(
        "org", "src", {"filename": "big.bin", "content_base64": huge}
    )
    assert status == 413
    assert body["max_bytes"] == settings.knowledge_max_file_bytes


def test_oversize_text_rejected_before_encode(monkeypatch):
    status, body = kroute._upload_document(
        "org", "src",
        {"filename": "big.txt",
         "text": "x" * (settings.knowledge_max_file_bytes + 1)},
    )
    assert status == 413


def test_small_payload_still_flows_past_the_cap(monkeypatch):
    # dal is stubbed to prove the request proceeds past the size checks.
    monkeypatch.setattr(kroute.dal, "upsert_document", lambda *a, **k: None)
    status, body = kroute._upload_document(
        "org", "src", {"filename": "ok.txt", "text": "hello"}
    )
    assert status == 404  # unknown source (stub) — i.e. we got past the cap
