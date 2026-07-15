"""RAG index stale-detection: editing a knowledge doc triggers a rebuild.

Before the per-source signature, an index built once stayed silently stale —
you could edit/add/remove a knowledge doc and retrieval kept answering from
the old content until someone manually ran ingest. Key-free (hash embedder).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import rag  # noqa: E402
from app.avatars import Avatar  # noqa: E402


def _avatar(tmp_path: Path) -> Avatar:
    (tmp_path / "knowledge").mkdir(exist_ok=True)
    return Avatar(
        id="stale-test",
        name="Laura",
        role="AI Process Expert",
        wake_words=["laura"],
        persona_prompt="",
        anam_avatar_id="r1",
        elevenlabs_voice_id="v1",
        min_confidence=0.55,
        speak_cooldown_seconds=8.0,
        dir=tmp_path,
    )


def _write_doc(avatar: Avatar, name: str, text: str) -> None:
    (avatar.knowledge_dir / name).write_text(text)


def _index_texts(avatar: Avatar) -> str:
    return " ".join(
        c["text"] for c in json.loads(avatar.index_path.read_text())["chunks"]
    )


def test_ensure_index_noop_when_docs_unchanged(tmp_path, monkeypatch):
    avatar = _avatar(tmp_path)
    _write_doc(avatar, "sop.md", "# Onboarding\n\nManager approval is required.")
    rag.ensure_index(avatar)

    calls = []
    monkeypatch.setattr(rag, "build_index", lambda a: calls.append(a.id))
    rag.ensure_index(avatar)
    assert calls == []  # current index → no rebuild


def test_ensure_index_rebuilds_when_doc_edited(tmp_path):
    avatar = _avatar(tmp_path)
    _write_doc(avatar, "sop.md", "# Onboarding\n\nManager approval is required.")
    rag.ensure_index(avatar)
    assert "security review" not in _index_texts(avatar)

    _write_doc(
        avatar, "sop.md", "# Onboarding\n\nA security review is required before go-live."
    )
    rag.ensure_index(avatar)
    assert "security review" in _index_texts(avatar)


def test_ensure_index_rebuilds_when_doc_added_or_removed(tmp_path):
    avatar = _avatar(tmp_path)
    _write_doc(avatar, "sop.md", "# Onboarding\n\nManager approval is required.")
    rag.ensure_index(avatar)

    _write_doc(avatar, "dpa.md", "# Legal\n\nThe DPA must be signed first.")
    rag.ensure_index(avatar)
    assert "DPA" in _index_texts(avatar)

    (avatar.knowledge_dir / "dpa.md").unlink()
    rag.ensure_index(avatar)
    assert "DPA" not in _index_texts(avatar)


def test_non_doc_files_never_trigger_rebuild(tmp_path, monkeypatch):
    avatar = _avatar(tmp_path)
    _write_doc(avatar, "sop.md", "# Onboarding\n\nManager approval is required.")
    rag.ensure_index(avatar)

    (avatar.knowledge_dir / ".DS_Store").write_bytes(b"junk")
    calls = []
    monkeypatch.setattr(rag, "build_index", lambda a: calls.append(a.id))
    rag.ensure_index(avatar)
    assert calls == []


def test_pre_signature_index_rebuilds_once(tmp_path):
    avatar = _avatar(tmp_path)
    _write_doc(avatar, "sop.md", "# Onboarding\n\nManager approval is required.")
    rag.ensure_index(avatar)

    # Simulate an index written before the sources signature existed.
    raw = json.loads(avatar.index_path.read_text())
    raw.pop("sources")
    avatar.index_path.write_text(json.dumps(raw))

    rag.ensure_index(avatar)
    assert "sources" in json.loads(avatar.index_path.read_text())
