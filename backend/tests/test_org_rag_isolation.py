"""Per-org RAG isolation: an org's ingested docs are ITS retrieval context.

The base pack (avatars/*/knowledge) stays shared and synthetic; real customer
documents land in a per-(org, avatar) index file. Isolation is structural —
org A's retrieve can never surface org B's documents — and the seam is fully
optional: no org_id (or no ingested docs) is byte-for-byte today's behavior.
"""
from __future__ import annotations

import pytest

from app import avatars, rag


ORG_A = "bf4a683b-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ORG_B = "bf4a683b-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture
def org_indexes(monkeypatch, tmp_path):
    """Point the org-index directory at tmp and ingest one doc per org."""
    monkeypatch.setattr("app.store.STORE_PATH", tmp_path / "store.sqlite3")
    rag._ORG_CACHE.clear()
    avatar = avatars.load("laura")

    doc_a = tmp_path / "acme_pricing.md"
    doc_a.write_text(
        "# Acme pricing playbook\n\nZebra-tier costs 42 doubloons per quarter."
    )
    doc_b = tmp_path / "globex_security.md"
    doc_b.write_text(
        "# Globex security review\n\nThe kraken firewall rotates keys hourly."
    )
    assert rag.build_org_index(avatar, ORG_A, [doc_a]) > 0
    assert rag.build_org_index(avatar, ORG_B, [doc_b]) > 0
    yield avatar
    rag._ORG_CACHE.clear()


def test_org_docs_retrievable_by_owner(org_indexes):
    avatar = org_indexes
    hits = rag.retrieve(avatar, "zebra tier doubloons pricing", k=3, org_id=ORG_A)
    assert any("doubloons" in h.text for h in hits)


def test_isolation_org_a_never_sees_org_b(org_indexes):
    avatar = org_indexes
    hits = rag.retrieve(avatar, "kraken firewall key rotation", k=6, org_id=ORG_A)
    assert not any("kraken" in h.text for h in hits)
    hits_b = rag.retrieve(avatar, "kraken firewall key rotation", k=6, org_id=ORG_B)
    assert any("kraken" in h.text for h in hits_b)


def test_no_org_id_is_base_pack_only(org_indexes):
    avatar = org_indexes
    hits = rag.retrieve(avatar, "zebra tier doubloons pricing", k=6)
    assert not any("doubloons" in h.text for h in hits)


def test_unknown_org_degrades_to_base(org_indexes):
    avatar = org_indexes
    base = rag.retrieve(avatar, "meeting process", k=3)
    scoped = rag.retrieve(avatar, "meeting process", k=3, org_id="never-ingested")
    assert [h.source for h in scoped] == [h.source for h in base]


def test_empty_ingest_removes_index(org_indexes):
    avatar = org_indexes
    assert rag.build_org_index(avatar, ORG_A, []) == 0
    assert not rag.org_index_path(avatar, ORG_A).exists()
    hits = rag.retrieve(avatar, "zebra tier doubloons", k=3, org_id=ORG_A)
    assert not any("doubloons" in h.text for h in hits)


def test_org_slug_never_escapes_directory():
    assert "/" not in rag._org_slug("../../etc/passwd")
    assert rag._org_slug("u_3f9a1c") == "u_3f9a1c"


def test_build_requires_org():
    avatar = avatars.load("laura")
    with pytest.raises(ValueError):
        rag.build_org_index(avatar, "", [])
