"""Data Foundation (DF0-DF1) release blockers on real Postgres as laura_app.

The binding v5 + acceptance-clarification matrix: RLS and composite-org FK
rejection; unknown-ACL fail-closed; connector eligibility hides records
immediately; same-body ACL revocation; cursor atomicity under backpressure;
durable quarantine + replay path; open-row raw DELETE denial; cross-org purge
denial; future-cutoff and young/open-row purge refusal; eligible
resolved-row purge with payload cleanup and audit completion; resolver
degradation never widening the M2 mask; authenticated-org mismatch rejection;
concurrent version/head consistency; structured records without the Company
Brain; and the live-index structural rule.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars, control_plane, rag, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.datafoundation import dal, resolver, sync  # noqa: E402
from app.datafoundation.envelope import body_checksum  # noqa: E402
from app.knowledge import dal as kdal  # noqa: E402
from app.knowledge import ingest as kingest  # noqa: E402
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
            "comment = 'test shim'\n"
        ),
        "pgcrypto--1.0.sql": "-- shim\n",
        "citext.control": (
            "default_version = '1.0'\nrelocatable = true\n"
            "comment = 'test shim'\n"
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
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("df_pg")))
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
    query = {k: str(info[k]) for k in ("host", "port")
             if info.get(k) is not None}
    app_sa_url = URL.create(
        "postgresql+psycopg", username=APP_ROLE, password="pw",
        database=info.get("dbname"), query=query,
    ).render_as_string(hide_password=False)
    app_uri = ci.make_conninfo(
        dbname=info.get("dbname"), user=APP_ROLE, password="pw",
        host=info.get("host"), port=info.get("port"),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env={**os.environ, "LAURA_DATABASE_ADMIN_URL": admin_sa_url,
             "LAURA_DATABASE_URL": "", "LAURA_REQUIRE_MIGRATIONS": "1"},
        capture_output=True, text=True, timeout=600,
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
    monkeypatch.setattr(settings, "data_foundation_enabled", True)
    monkeypatch.setattr(settings, "company_brain_enabled", True)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    rag._ORG_CACHE.clear()
    rag._ORG_MISS.clear()
    control_plane.reset_engine()
    with _admin(pg) as conn:
        # df_connectors cascades records -> versions/ACLs (the circular head
        # FK is DEFERRABLE, checked at commit when both sides are gone).
        for table in ("df_connectors", "df_purge_audit",
                      "knowledge_sync_jobs", "knowledge_chunks",
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


def _env(eid: str, *, acl_mode: str = "org_default", acl=None,
         body: str = "hello", deleted: bool = False, title: str = "",
         container: str = "") -> dict:
    return {
        "external_id": eid, "kind": "document", "title": title or eid,
        "container_external_id": container, "external_updated_at": "",
        "acl_mode": acl_mode, "acl": acl or [], "deleted": deleted,
        "checksum": body_checksum(body),
    }


def _connector(org: str, kind: str = "gdrive", **kwargs) -> dict:
    row = dal.create_connector(org, kind, f"{kind} test", **kwargs)
    assert row is not None
    return row


# ── RLS + composite-org FK rejection ────────────────────────────────────────

def test_rls_and_cross_org_fk_rejection(cp, pg):
    org_a = _org(cp, "rls-a")
    org_b = _org(cp, "rls-b")
    conn_a = _connector(org_a)
    dal.commit_batch(org_a, conn_a["id"], [_env("r1")], new_cursor=None)

    with psycopg.connect(pg["app_uri"], autocommit=True) as raw:
        raw.execute("SELECT set_config('app.current_org', %s, false)",
                    (org_b,))
        for table in ("df_connectors", "df_source_records",
                      "df_source_record_versions", "df_acl_entries"):
            assert raw.execute(
                f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        denied = raw.execute(
            "UPDATE df_source_records SET tombstoned=true WHERE org_id=%s",
            (org_a,))
        assert denied.rowcount == 0

    # Composite FK: a cross-org current_version_id fails at the DATABASE.
    with _admin(pg) as admin:
        rec_b = admin.execute(
            "INSERT INTO df_connectors (org_id, kind, name) "
            "VALUES (%s, 'upload', 'x') RETURNING id", (org_b,)
        ).fetchone()[0]
        ver_a = admin.execute(
            "SELECT id FROM df_source_record_versions WHERE org_id=%s LIMIT 1",
            (org_a,)).fetchone()[0]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            admin.execute(
                "INSERT INTO df_source_records "
                "(org_id, connector_id, external_id, kind, "
                " current_version_id) "
                "VALUES (%s, %s, 'evil', 'document', %s)",
                (org_b, rec_b, ver_a))


# ── ACL fail-closed + eligibility + revocation ──────────────────────────────

def test_unknown_acl_exposes_nothing(cp):
    org = _org(cp, "unk")
    connector = _connector(org)
    dal.commit_batch(org, connector["id"],
                     [_env("u1", acl_mode="unknown")], new_cursor=None)
    assert dal.visible_heads(org) == []
    ids, complete = dal.principal_identity_closure(org, "u_whoever")
    assert dal.visible_heads(org, principal_identity_ids=ids) == []
    assert complete is True


def test_connector_eligibility_hides_records_immediately(cp):
    org = _org(cp, "elig")
    connector = _connector(org)
    dal.commit_batch(org, connector["id"], [_env("e1")], new_cursor=None)
    assert len(dal.visible_heads(org)) == 1
    # Pause: previously visible records vanish with NO record rewrite.
    dal.set_connector_status(org, connector["id"], "paused", "test")
    assert dal.visible_heads(org) == []
    dal.set_connector_status(org, connector["id"], "active", "test")
    assert len(dal.visible_heads(org)) == 1
    # Scope loss (needs_reconnect) hides mirrored content the same way.
    dal.set_connector_status(org, connector["id"], "needs_reconnect", "test")
    assert dal.visible_heads(org) == []


def test_same_body_acl_revocation_takes_effect(cp):
    org = _org(cp, "revoke")
    connector = _connector(org, trusted_email_issuer=True)
    dal.set_connector_acl_mirrored(org, connector["id"], True)
    principal = "u_alice"
    store_email = f"alice-{time.time_ns()}@x.co"
    acl = [{"principal_kind": "user", "principal_external_id": store_email,
            "access": "reader"}]
    dal.commit_batch(
        org, connector["id"],
        [_env("m1", acl_mode="mirrored", acl=acl, body="same body")],
        new_cursor=None,
        identities=[{"external_id": store_email, "kind": "user",
                     "email": store_email, "email_verified": True}],
        trusted_email_issuer=True,
    )
    # Bind explicitly (deterministic, independent of the email path).
    identity_rows = dal.visible_heads(org)  # none yet for org (mirrored only)
    assert identity_rows == []
    with_ids, _ = dal.principal_identity_closure(org, principal)
    if not with_ids:
        # explicit admin binding
        from sqlalchemy import text as sql

        engine = control_plane._get_engine()
        with engine.begin() as conn:
            control_plane._set_org(conn, org)
            iid = conn.execute(
                sql("SELECT id::text FROM df_identities "
                    "WHERE org_id=:o AND external_id=:e"),
                {"o": org, "e": store_email},
            ).first()[0]
        dal.bind_identity(org, iid, principal, "admin")
        with_ids, _ = dal.principal_identity_closure(org, principal)
    assert len(dal.visible_heads(
        org, principal_identity_ids=with_ids)) == 1

    # Same body checksum, ACL revoked (mirrored, different principal):
    other = [{"principal_kind": "user", "principal_external_id": "bob@x.co",
              "access": "reader"}]
    stats = dal.commit_batch(
        org, connector["id"],
        [_env("m1", acl_mode="mirrored", acl=other, body="same body")],
        new_cursor=None,
    )
    assert stats["meta_updated"] == 1  # applied despite unchanged checksum
    assert dal.visible_heads(org, principal_identity_ids=with_ids) == []


# ── cursor atomicity + backpressure ─────────────────────────────────────────

def test_cursor_atomicity_under_backpressure(cp, monkeypatch):
    org = _org(cp, "cursor")
    connector = _connector(org)
    monkeypatch.setattr(dal, "_QUARANTINE_OPEN_CAP", 1)
    # First batch: one invalid envelope quarantines (cap 1 reached), cursor
    # advances because the batch itself was accepted.
    stats = dal.commit_batch(
        org, connector["id"],
        [{"external_id": "bad1", "kind": "nope", "deleted": False}],
        new_cursor={"page_token": 5},
    )
    assert stats["quarantined"] == 1
    assert dal.get_cursor(org, connector["id"]) == {"page_token": 5}
    # Second batch would exceed the cap: EVERYTHING rolls back; the valid
    # envelope is not applied and the cursor stays at 5.
    with pytest.raises(dal.BackpressureError):
        dal.commit_batch(
            org, connector["id"],
            [_env("good1"),
             {"external_id": "bad2", "kind": "nope", "deleted": False}],
            new_cursor={"page_token": 9},
        )
    assert dal.get_cursor(org, connector["id"]) == {"page_token": 5}
    assert dal.visible_heads(org) == []  # good1 rolled back with the batch


# ── durable quarantine: replay path, raw-DELETE denial, purge rules ─────────

def test_quarantine_replay_and_deletion_boundaries(cp, pg, tmp_path,
                                                   monkeypatch):
    org = _org(cp, "quar")
    connector = _connector(org)
    payload = {"external_id": "q1", "kind": "nope", "deleted": False}
    dal.commit_batch(
        org, connector["id"], [payload], new_cursor=None,
        quarantine_payload_ref=sync._payload_ref_writer(org),
    )
    rows = dal.quarantine_rows(org, state="open")
    assert len(rows) == 1 and rows[0]["payload_ref"]
    qid = rows[0]["id"]

    # Replay goes through the NORMAL upsert path; a still-invalid payload
    # re-quarantines (durable + inspectable, nothing lost).
    from app.datafoundation import router as df_router

    code, body = df_router._op_quarantine_replay(org, qid, "tester")
    assert code == 409 and "invalid" in body["error"]
    assert dal.quarantine_rows(org, state="open")  # still open, not lost

    # Raw SQL as laura_app: DELETE of an OPEN row is DENIED at the database.
    with psycopg.connect(pg["app_uri"], autocommit=True) as raw:
        raw.execute("SELECT set_config('app.current_org', %s, false)", (org,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            raw.execute("DELETE FROM df_quarantine")

    # Cross-org purge denial: context org A, argument org B -> exception.
    org_b = _org(cp, "quar-b")
    from sqlalchemy import text as sql

    engine = control_plane._get_engine()
    with pytest.raises(Exception) as excinfo:
        with engine.begin() as conn:
            control_plane._set_org(conn, org)
            conn.execute(
                sql("SELECT laura_private.purge_quarantine("
                    "CAST(:o AS uuid), NULL)"),
                {"o": org_b},
            )
    assert "does not match transaction context" in str(excinfo.value)

    # Resolve the row, but YOUNG: a FUTURE cutoff must not purge it (the
    # server clamps to the org retention policy).
    dal.resolve_quarantine(org, qid, "discarded", "tester")
    future = time.time() + 10 * 24 * 3600
    assert dal.purge_quarantine(org, future) == 0
    assert dal.quarantine_rows(org, state="discarded")

    # Age it beyond retention (admin clock surgery; the org policy is pinned
    # to 30 days; the definer reads THIS, proving the cutoff is the org's
    # server-side policy, not the caller's argument), purge, verify payload
    # cleanup + audit completion (two-phase).
    with _admin(pg) as admin:
        admin.execute(
            "UPDATE orgs SET retention_days=30 WHERE id=%s", (org,))
        admin.execute(
            "UPDATE df_quarantine SET resolved_at = "
            "clock_timestamp() - interval '40 days' WHERE org_id=%s", (org,))
    assert dal.purge_quarantine(org) == 1
    # ONLY the resolved row purged; the OPEN row from the failed replay
    # survives even though the admin aged its clock; open rows are
    # structurally unpurgeable (the fixed state predicate).
    remaining = dal.quarantine_rows(org)
    assert remaining and all(r["state"] == "open" for r in remaining)
    assert all(r["id"] != qid for r in remaining)
    audits = dal.pending_purge_audits(org)
    assert audits and audits[0]["payload_refs_pending"]
    ref = audits[0]["payload_refs_pending"][0]
    from app.knowledge import storage as kstorage

    assert kstorage.get_bytes(ref) is not None
    assert sync.complete_purge_audits(org) == 1
    assert kstorage.get_bytes(ref) is None  # payload actually deleted
    assert dal.pending_purge_audits(org) == []


# ── resolver: no widening, non-disclosure, M2 parity ────────────────────────

def test_resolver_degradation_never_widens(cp, monkeypatch):
    org = _org(cp, "deg")
    avatars.load("laura")  # canonical exists
    base = resolver._m2_chunks(org, "laura", "refunds", 4)

    def boom(*a, **k):
        raise RuntimeError("df down")

    monkeypatch.setattr(dal, "visible_heads", boom)
    result = resolver.resolve(org, "laura", "refunds", k=4,
                              principal_id="u_x")
    assert result["resolution"]["degraded"] is True
    assert [c["text"] for c in result["chunks"]] == [c["text"] for c in base]
    assert "acl_filtered_count" not in json.dumps(result)


def test_resolver_sees_org_default_only_without_principal(cp):
    org = _org(cp, "resv")
    connector = _connector(org)
    dal.set_connector_acl_mirrored(org, connector["id"], True)
    dal.commit_batch(
        org, connector["id"],
        [_env("open-doc", title="Quarterly refunds guide"),
         _env("priv-doc", acl_mode="mirrored", title="Secret refunds memo",
              acl=[{"principal_kind": "user",
                    "principal_external_id": "x@y.z", "access": "reader"}])],
        new_cursor=None,
    )
    result = resolver.resolve(org, "laura", "refunds guide", k=6)
    texts = json.dumps(result["chunks"])
    assert "Quarterly refunds guide" in texts
    assert "Secret refunds memo" not in texts
    assert result["freshness"]["stale_connectors"] is not None


# ── concurrency: version/head consistency ───────────────────────────────────

def test_concurrent_upserts_keep_head_consistent(cp):
    org = _org(cp, "conc")
    connector = _connector(org)

    def write(body: str):
        return dal.commit_batch(org, connector["id"],
                                [_env("c1", body=body)], new_cursor=None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, ["body-A", "body-B"]))
    heads = dal.visible_heads(org)
    assert len(heads) == 1
    from sqlalchemy import text as sql

    engine = control_plane._get_engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org)
        rows = conn.execute(
            sql("SELECT version_no FROM df_source_record_versions "
                "WHERE org_id=:o ORDER BY version_no"),
            {"o": org},
        ).fetchall()
    version_nos = [r[0] for r in rows]
    assert version_nos == list(range(1, len(version_nos) + 1))


# ── tombstone / resurrection / idempotency ──────────────────────────────────

def test_tombstone_resurrection_and_idempotency(cp):
    org = _org(cp, "tomb")
    connector = _connector(org)
    s1 = dal.commit_batch(org, connector["id"], [_env("t1")], new_cursor=None)
    assert s1["created"] == 1
    s2 = dal.commit_batch(org, connector["id"], [_env("t1")], new_cursor=None)
    assert s2["unchanged"] == 1  # idempotent by checksum + meta
    s3 = dal.commit_batch(org, connector["id"],
                          [_env("t1", deleted=True)], new_cursor=None)
    assert s3["tombstoned"] == 1
    assert dal.visible_heads(org) == []
    s4 = dal.commit_batch(org, connector["id"],
                          [_env("t1", body="new life")], new_cursor=None)
    assert s4["resurrected"] == 1
    heads = dal.visible_heads(org)
    assert len(heads) == 1
    lineage = heads[0]["lineage_json"]
    if isinstance(lineage, str):
        lineage = json.loads(lineage)
    assert "resurrected_from_version" in lineage


# ── structured records without the Company Brain ────────────────────────────

def test_structured_records_work_without_brain(cp, monkeypatch):
    monkeypatch.setattr(settings, "company_brain_enabled", False)
    org = _org(cp, "nobrain")
    connector = _connector(org)
    stats = dal.commit_batch(org, connector["id"],
                             [_env("s1", title="CRM row")], new_cursor=None)
    assert stats["created"] == 1
    assert len(dal.visible_heads(org)) == 1
    # Body-bearing envelopes quarantine with the exact contract reason.
    materialized = sync.materialize_bodies(
        org, connector, [{**_env("s2"), "body_text": "long body"}])
    stats = dal.commit_batch(org, connector["id"], materialized,
                             new_cursor=None)
    assert stats["quarantined"] == 1
    rows = dal.quarantine_rows(org, state="open")
    assert any("body_pipeline_disabled" in r["reason"] for r in rows)


# ── org mismatch rejection at the HTTP layer ────────────────────────────────

def test_client_supplied_org_id_is_rejected(cp, monkeypatch):
    import importlib

    import app.main as main_module
    from fastapi.testclient import TestClient

    from app import auth

    client = TestClient(main_module.app)
    user = store.upsert_user("owner-df@x.co")
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    r = client.post(
        "/dashboard/data/resolve",
        json={"avatar_key": "laura", "query": "q",
              "org_id": "11111111-1111-1111-1111-111111111111"},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "org mismatch"
    _ = importlib  # imported for parity with sibling tests


# ── live-index structural rule ──────────────────────────────────────────────

# ── adversarial-verification regressions (six holes found + fixed) ──────────

def test_runtime_cannot_backdate_resolved_at_to_fast_purge(cp, pg):
    """HIGH: laura_app has no direct UPDATE on df_quarantine, so it cannot set
    resolved_at into the past to fast-track a purge."""
    org = _org(cp, "backdate")
    connector = _connector(org)
    dal.commit_batch(
        org, connector["id"],
        [{"external_id": "q", "kind": "nope", "deleted": False}],
        new_cursor=None,
        quarantine_payload_ref=sync._payload_ref_writer(org),
    )
    qid = dal.quarantine_rows(org, state="open")[0]["id"]
    with psycopg.connect(pg["app_uri"], autocommit=True) as raw:
        raw.execute("SELECT set_config('app.current_org', %s, false)", (org,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            raw.execute(
                "UPDATE df_quarantine SET state='discarded', "
                "resolved_at=clock_timestamp() - interval '40 days'")
    # The sanctioned resolve stamps resolved_at server-side (now), so the row
    # is young and the retention clamp protects it.
    assert dal.resolve_quarantine(org, qid, "discarded", "tester")
    assert dal.purge_quarantine(org) == 0  # freshly resolved => not aged
    assert dal.quarantine_rows(org)  # survives


def test_purge_versions_keeps_max_even_if_head_nulled(cp, pg):
    """MEDIUM: NULLing current_version_id cannot make the live (max) version
    purgeable — the predicate is structural (never the max version_no)."""
    org = _org(cp, "headnull")
    connector = _connector(org)
    dal.commit_batch(org, connector["id"], [_env("v", body="one")],
                     new_cursor=None)
    dal.commit_batch(org, connector["id"], [_env("v", body="two")],
                     new_cursor=None)
    with _admin(pg) as admin:
        admin.execute("UPDATE orgs SET retention_days=30 WHERE id=%s", (org,))
        admin.execute(
            "UPDATE df_source_record_versions SET ingested_at="
            "clock_timestamp() - interval '40 days' WHERE org_id=%s", (org,))
    # Runtime nulls the head, then purges.
    with psycopg.connect(pg["app_uri"], autocommit=True) as raw:
        raw.execute("SELECT set_config('app.current_org', %s, false)", (org,))
        raw.execute("UPDATE df_source_records SET current_version_id=NULL "
                    "WHERE org_id=%s", (org,))
    dal.purge_record_versions(org)
    from sqlalchemy import text as sql

    engine = control_plane._get_engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org)
        remaining = conn.execute(
            sql("SELECT max(version_no) FROM df_source_record_versions "
                "WHERE org_id=:o"), {"o": org},
        ).first()[0]
    assert remaining == 2  # the highest version is structurally protected


def test_connector_get_never_returns_credential_ref(cp):
    """MEDIUM: credential_ref is a secret pointer — never in a response."""
    org = _org(cp, "cred")
    dal.create_connector(org, "gdrive", "leaky",
                         credential_ref="ssm:/laura/refresh=SECRET")
    connectors = dal.list_connectors(org)
    cid = connectors[0]["id"]
    from app.datafoundation import router as df_router

    code, body = df_router._op_connector_get(org, cid)
    assert code == 200
    wire = json.dumps(df_router._jsonable(body))  # the real response boundary
    assert "SECRET" not in wire
    assert "credential_ref" not in wire
    assert body["connector"].get("has_credential") is True


def test_keyword_search_also_excludes_mirrored_df_bodies(cp):
    """MEDIUM: the M2 keyword search read path applies the DF restriction."""
    org = _org(cp, "kwsearch")
    connector = _connector(org)
    dal.set_connector_acl_mirrored(org, connector["id"], True)
    envs = sync.materialize_bodies(
        org, {"id": connector["id"], "kind": "gdrive", "name": "g"},
        [{**_env("priv", acl_mode="mirrored",
                 acl=[{"principal_kind": "user",
                       "principal_external_id": "a@b.c", "access": "reader"}]),
          "body_text": "Bonuses are paid in DOUBLOONS quarterly."}])
    dal.commit_batch(org, connector["id"], envs, new_cursor=None)
    for source in kdal.list_sources(org):
        if source["name"].startswith("df:"):
            assert kdal.assign(org, source["id"], "laura")
    hits = kdal.keyword_search(org, "DOUBLOONS", avatar_id="laura")
    assert all("DOUBLOONS" not in h["text"] for h in hits)


def test_orphaned_df_body_chunks_are_restricted(cp):
    """HIGH: body chunks published BEFORE commit_batch (park/crash/race) have
    no DF head; they must be restricted from the live index, not exposed."""
    org = _org(cp, "orphan")
    connector = _connector(org)
    # materialize WITHOUT commit_batch; the exact park/crash window.
    sync.materialize_bodies(
        org, {"id": connector["id"], "kind": "gdrive", "name": "g"},
        [{**_env("stray"),
          "body_text": "Refunds secretly take NINETY days."}])
    for source in kdal.list_sources(org):
        if source["name"].startswith("df:"):
            assert kdal.assign(org, source["id"], "laura")
    chunks = kdal.chunks_for_avatar(org, "laura")
    assert all("NINETY days" not in c["text"] for c in chunks)
    hits = kdal.keyword_search(org, "NINETY", avatar_id="laura")
    assert all("NINETY" not in h["text"] for h in hits)


def test_live_index_excludes_non_org_default_df_documents(cp, monkeypatch):
    org = _org(cp, "liveidx")
    # A real Company Brain doc assigned to laura (org_default via upload).
    src = kdal.create_source(org, "Handbook", "upload")
    code, payload = knowledge_router._upload_document(
        org, src["id"], {"filename": "open.md",
                         "text": "# Open\n\nRefunds take THREE days."})
    assert code == 200, payload
    assert kdal.assign(org, src["id"], "laura")
    for _ in range(6):
        if kingest.process_due() == 0:
            break

    # A mirrored-private DF body doc in ANOTHER source via the body pipeline.
    connector = _connector(org)
    dal.set_connector_acl_mirrored(org, connector["id"], True)
    envs = sync.materialize_bodies(
        org, {"id": connector["id"], "kind": "gdrive", "name": "g"},
        [{**_env("priv", acl_mode="mirrored",
                 acl=[{"principal_kind": "user",
                       "principal_external_id": "a@b.c",
                       "access": "reader"}]),
          "body_text": "# Secret\n\nRefunds secretly take NINETY days."}])
    dal.commit_batch(org, connector["id"], envs, new_cursor=None)
    # Assign the DF body source to laura too; the ACL rule must STILL keep
    # it out of the live index (structural, not assignment-dependent).
    for source in kdal.list_sources(org):
        if source["name"].startswith("df:"):
            assert kdal.assign(org, source["id"], "laura")
    chunks = kdal.chunks_for_avatar(org, "laura")
    texts = " ".join(c["text"] for c in chunks)
    assert "THREE days" in texts
    assert "NINETY days" not in texts  # mirrored content structurally absent
