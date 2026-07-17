"""Company Brain (M1) acceptance on real Postgres as laura_app.

The milestone gate from docs/product/LAURA-COMPANY-BRAIN-SKILLS-BROWSER-
ROADMAP.md: two orgs upload similarly named private docs; each avatar
retrieves ONLY its own assigned source with correct citations; deleting a
source removes it from future retrieval. Plus the version-immutability /
checksum-dedupe rule, the claim/lease job worker, and raw FORCE-RLS checks.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars, control_plane, rag, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.knowledge import dal, ingest  # noqa: E402
from app.knowledge import router as knowledge_router  # noqa: E402

pytestmark = pytest.mark.pg
BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_ROLE = "laura_app"


def _write_extension_shims() -> None:
    ext_dir = (
        Path(pgserver.__file__).parent
        / "pginstall" / "share" / "postgresql" / "extension"
    )
    shims = {
        "pgcrypto.control": (
            "default_version = '1.0'\nrelocatable = true\n"
            "comment = 'test shim: gen_random_uuid() is core since PG13'\n"
        ),
        "pgcrypto--1.0.sql": "-- shim: no objects; gen_random_uuid() is core\n",
        "citext.control": (
            "default_version = '1.0'\nrelocatable = true\n"
            "comment = 'test shim: citext as a plain-text domain'\n"
        ),
        "citext--1.0.sql": "CREATE DOMAIN citext AS text;\n",
    }
    for name, body in shims.items():
        path = ext_dir / name
        if not path.exists():
            path.write_text(body)


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    _write_extension_shims()
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("brain_pg")))
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(
            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'pw' "
            "NOSUPERUSER NOBYPASSRLS"
        )

    import psycopg.conninfo as ci
    from sqlalchemy.engine import URL

    admin_sa_url = uri.replace("postgresql://", "postgresql+psycopg://")
    info = ci.conninfo_to_dict(uri)
    query = {
        key: str(info[key])
        for key in ("host", "port")
        if info.get(key) is not None
    }
    app_sa_url = URL.create(
        "postgresql+psycopg",
        username=APP_ROLE,
        password="pw",
        database=info.get("dbname"),
        query=query,
    ).render_as_string(hide_password=False)
    app_uri = ci.make_conninfo(
        dbname=info.get("dbname"), user=APP_ROLE, password="pw",
        host=info.get("host"), port=info.get("port"),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env={
            **os.environ,
            "LAURA_DATABASE_ADMIN_URL": admin_sa_url,
            "LAURA_DATABASE_URL": "",
            "LAURA_REQUIRE_MIGRATIONS": "1",
        },
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"alembic upgrade failed:\n{proc.stdout}\n{proc.stderr}"
    )
    yield {"uri": uri, "app_sa_url": app_sa_url, "app_uri": app_uri}
    control_plane.reset_engine()
    srv.cleanup()


def _admin(pg):
    return psycopg.connect(pg["uri"], autocommit=True)


@pytest.fixture
def cp(pg, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    monkeypatch.setattr(settings, "company_brain_enabled", True)
    # Org index files + local file storage land in a throwaway dir, and the
    # in-process rag caches must not leak across tests.
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    rag._ORG_CACHE.clear()
    rag._ORG_MISS.clear()
    control_plane.reset_engine()
    with _admin(pg) as conn:
        for table in ("knowledge_sync_jobs", "knowledge_chunks",
                      "knowledge_document_versions", "knowledge_documents",
                      "knowledge_assignments", "knowledge_sources"):
            conn.execute(f"DELETE FROM {table}")
    yield control_plane
    rag._ORG_CACHE.clear()
    rag._ORG_MISS.clear()
    control_plane.reset_engine()


def _org(cp, tag: str) -> str:
    stamp = str(time.time_ns())
    return cp.ensure_user(
        f"sub-{tag}-{stamp}", f"{tag}-{stamp}@freemail.test", tag, ""
    )["org_id"]


def _drain_jobs(rounds: int = 6) -> None:
    for _ in range(rounds):
        if ingest.process_due() == 0:
            return


def _upload(org: str, source_id: str, filename: str, text: str) -> str:
    code, payload = knowledge_router._upload_document(
        org, source_id, {"filename": filename, "text": text}
    )
    assert code == 200, payload
    return payload["document_id"]


def test_two_org_isolation_with_citations(cp):
    org_a = _org(cp, "brain-a")
    org_b = _org(cp, "brain-b")
    src_a = dal.create_source(org_a, "Handbook", "upload")
    src_b = dal.create_source(org_b, "Handbook", "upload")

    _upload(org_a, src_a["id"], "handbook.md",
            "# Refunds\n\nAcme alpha refunds take THREE business days.")
    _upload(org_b, src_b["id"], "handbook.md",
            "# Refunds\n\nBeta Corp refunds take NINETY business days.")
    assert dal.assign(org_a, src_a["id"], "laura")
    assert dal.assign(org_b, src_b["id"], "laura")
    _drain_jobs()

    avatar = avatars.load("laura")
    hits_a = rag.retrieve(avatar, "how long do refunds take", k=4,
                          org_id=org_a)
    text_a = " ".join(h.text for h in hits_a)
    assert "THREE business days" in text_a
    assert "NINETY" not in text_a
    assert any(h.source == "handbook.md" for h in hits_a)

    hits_b = rag.retrieve(avatar, "how long do refunds take", k=4,
                          org_id=org_b)
    text_b = " ".join(h.text for h in hits_b)
    assert "NINETY business days" in text_b
    assert "THREE business days" not in text_b

    # Keyword (FTS) half is isolated the same way.
    kw_a = dal.keyword_search(org_a, "refunds", avatar_id="laura")
    assert kw_a and all("NINETY" not in r["text"] for r in kw_a)


def test_deletion_removes_retrievability(cp):
    org = _org(cp, "brain-del")
    src = dal.create_source(org, "Secrets", "upload")
    _upload(org, src["id"], "policy.md",
            "# Policy\n\nThe launch codeword is ZANZIBAR-SUNSET.")
    assert dal.assign(org, src["id"], "laura")
    _drain_jobs()

    avatar = avatars.load("laura")
    before = " ".join(
        h.text for h in rag.retrieve(avatar, "launch codeword", k=4, org_id=org)
    )
    assert "ZANZIBAR-SUNSET" in before

    assert dal.delete_source(org, src["id"])
    _drain_jobs()
    after = " ".join(
        h.text for h in rag.retrieve(avatar, "launch codeword", k=4, org_id=org)
    )
    assert "ZANZIBAR-SUNSET" not in after
    assert dal.keyword_search(org, "ZANZIBAR", avatar_id="laura") == []


def test_versions_are_immutable_and_deduped(cp):
    org = _org(cp, "brain-ver")
    src = dal.create_source(org, "Docs", "upload")
    doc_id = _upload(org, src["id"], "sop.md", "# SOP\n\nStep one: breathe.")
    _drain_jobs()

    # Same content again → dedupe, still version 1.
    _upload(org, src["id"], "sop.md", "# SOP\n\nStep one: breathe.")
    _drain_jobs()
    docs = dal.list_documents(org, src["id"])
    assert docs[0]["latest_version"] == 1

    # Changed content → NEW immutable version 2; version 1 text survives.
    _upload(org, src["id"], "sop.md", "# SOP\n\nStep one: exhale slowly.")
    _drain_jobs()
    docs = dal.list_documents(org, src["id"])
    assert docs[0]["latest_version"] == 2
    engine = control_plane._get_engine()
    from sqlalchemy import text as sql

    with engine.begin() as conn:
        control_plane._set_org(conn, org)
        rows = conn.execute(
            sql(
                "SELECT version, text_content FROM knowledge_document_versions "
                "WHERE org_id=:o AND document_id=CAST(:d AS uuid) "
                "ORDER BY version"
            ),
            {"o": org, "d": doc_id},
        ).fetchall()
    assert [r[0] for r in rows] == [1, 2]
    assert "breathe" in rows[0][1] and "exhale" in rows[1][1]


def test_job_claim_lease_and_retry(cp, monkeypatch):
    org = _org(cp, "brain-jobs")
    src = dal.create_source(org, "Broken", "upload")
    doc_id = _upload(org, src["id"], "broken.pdf", "not really a pdf")

    # Make the stored bytes unavailable so ingest fails deterministically.
    from app.knowledge import storage as knowledge_storage

    monkeypatch.setattr(knowledge_storage, "get_bytes", lambda ref: None)
    jobs = dal.claim_due_jobs(org, 4)
    assert jobs and jobs[0]["kind"] == "ingest_document"
    # Claimed jobs are leased: a second claim in the window gets nothing.
    assert dal.claim_due_jobs(org, 4) == []
    assert dal.finish_job(
        org, jobs[0]["id"], jobs[0]["lease_token"], ok=False,
        error="stored bytes unavailable", attempts=jobs[0]["attempts"],
    )
    rows = dal.job_rows(org)
    failed = [r for r in rows if r["id"] == jobs[0]["id"]][0]
    assert failed["status"] == "failed"
    assert "unavailable" in failed["last_error"]
    assert doc_id  # document exists; a later retry can heal it


def test_rls_isolates_knowledge_tables(cp, pg):
    org_a = _org(cp, "brain-rls-a")
    org_b = _org(cp, "brain-rls-b")
    src = dal.create_source(org_a, "Private", "upload")
    _upload(org_a, src["id"], "private.md", "# P\n\nprivate alpha text")
    _drain_jobs()

    with psycopg.connect(pg["app_uri"], autocommit=True) as conn:
        conn.execute(
            "SELECT set_config('app.current_org', %s, false)", (org_b,)
        )
        for table in ("knowledge_sources", "knowledge_documents",
                      "knowledge_document_versions", "knowledge_chunks"):
            rows = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            assert rows[0] == 0, f"{table} leaked across orgs"
        denied = conn.execute(
            "UPDATE knowledge_sources SET name='pwned' WHERE org_id=%s",
            (org_a,),
        )
        assert denied.rowcount == 0
