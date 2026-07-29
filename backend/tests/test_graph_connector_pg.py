"""Microsoft Graph connector — ACL/RLS semantics on real Postgres as laura_app.

Drives a Graph sync end-to-end through sync.process_due (list/delta → connector
→ commit_batch → cursor advance) and asserts the visibility rule holds for
Graph-emitted records: mirrored ACLs, inherited container grants, nested-group
closure, verified-email identity mapping, unknown-ACL default deny, delta
tombstone revocation, cross-org isolation, 429 cursor safety, and no-secret
persistence. Complements the pure connector logic in test_graph_connector.py.
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

from app import control_plane, rag, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.datafoundation import dal, graph, sync  # noqa: E402

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
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("graph_pg")))
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
    monkeypatch.setattr(settings, "graph_connector_enabled", True)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    rag._ORG_CACHE.clear()
    rag._ORG_MISS.clear()
    control_plane.reset_engine()
    with _admin(pg) as conn:
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


def _fixture(**overrides) -> dict:
    base = {
        "page_size": 2,
        "latest_delta_token": 5,
        "items": {
            "doc-open": {"name": "Onboarding", "body": "everyone onboarding",
                         "container": "site-a", "author": "u-alice",
                         "org_wide": True},
            "doc-hr": {"name": "HR bands", "body": "salary bands hr",
                       "container": "site-hr",
                       "acl": [{"principal_kind": "group",
                                "principal_external_id": "g-hr",
                                "access": "reader"}]},
            "doc-inherit": {"name": "Eng plan", "body": "eng roadmap",
                            "container": "site-a", "inherits_from": "site-a"},
            "doc-secret": {"name": "Sealed", "body": "unreadable perms",
                           "permissions_readable": False},
        },
        "containers": {
            "site-a": {"acl": [{"principal_kind": "group",
                                "principal_external_id": "g-eng",
                                "access": "reader"}]},
        },
        "principals": {
            "users": [
                {"id": "u-alice", "display": "Alice",
                 "email": "alice@acme.test", "email_verified": True},
                {"id": "u-bob", "display": "Bob",
                 "email": "bob@acme.test", "email_verified": True},
            ],
            "groups": [
                {"id": "g-hr", "display": "HR",
                 "members": ["u-bob", "g-leads"]},
                {"id": "g-leads", "display": "Leads", "members": ["u-alice"]},
                {"id": "g-eng", "display": "Eng", "members": ["u-alice"]},
            ],
        },
        "deltas": [{"token": 5, "item": "doc-open", "op": "remove"}],
    }
    base.update(overrides)
    return base


def _graph_connector(org: str, fixture: dict, *, trusted: bool = True) -> dict:
    row = dal.create_connector(
        org, "graph", "M365", config={"graph_fixture": fixture},
        trusted_email_issuer=trusted,
    )
    assert row is not None
    return row


def _run_full(org: str, connector_id: str) -> None:
    dal.enqueue_run(org, connector_id, "full")
    sync.process_due()


def _titles(heads: list[dict]) -> set[str]:
    return {h["title"] for h in heads}


def _closure_heads(org: str, email: str) -> list[dict]:
    principal = store.user_id_for_email(email)
    ids, _complete = dal.principal_identity_closure(org, principal)
    return dal.visible_heads(org, principal_identity_ids=ids)


# ── full sync → ACL visibility ──────────────────────────────────────────────

def test_graph_full_sync_commits_and_org_default_visible(cp):
    org = _org(cp, "gfull")
    conn = _graph_connector(org, _fixture(deltas=[]))
    _run_full(org, conn["id"])
    # A stranger (no identity closure) sees only the org-wide document.
    strangers = dal.visible_heads(org, principal_identity_ids=set())
    assert _titles(strangers) == {"Onboarding"}


def test_graph_nested_group_closure_and_inherited_acl(cp):
    org = _org(cp, "gnest")
    conn = _graph_connector(org, _fixture(deltas=[]))
    _run_full(org, conn["id"])
    # Alice: member of g-leads (⊂ g-hr) and g-eng. She sees the org-wide doc,
    # the HR doc via the NESTED group, and the Eng doc via the INHERITED grant.
    alice = _titles(_closure_heads(org, "alice@acme.test"))
    assert alice == {"Onboarding", "HR bands", "Eng plan"}
    # Bob: direct g-hr member only → org-wide + HR, never the Eng-inherited doc.
    bob = _titles(_closure_heads(org, "bob@acme.test"))
    assert bob == {"Onboarding", "HR bands"}


def test_graph_unknown_acl_is_default_deny(cp):
    org = _org(cp, "gdeny")
    conn = _graph_connector(org, _fixture(deltas=[]))
    _run_full(org, conn["id"])
    # doc-secret (permissions unreadable → unknown) is visible to NOBODY,
    # including the most-privileged principal.
    for email in ("alice@acme.test", "bob@acme.test"):
        assert "Sealed" not in _titles(_closure_heads(org, email))
    assert "Sealed" not in _titles(
        dal.visible_heads(org, principal_identity_ids=set())
    )


def test_graph_verified_email_identity_mapping(cp):
    org = _org(cp, "gmap")
    conn = _graph_connector(org, _fixture(deltas=[]), trusted=True)
    _run_full(org, conn["id"])
    # trusted_email_issuer + email_verified auto-bound Alice's Entra identity to
    # her Laura principal — her closure is non-empty without any admin action.
    principal = store.user_id_for_email("alice@acme.test")
    ids, complete = dal.principal_identity_closure(org, principal)
    assert ids and complete
    # An UNtrusted issuer must NOT auto-bind (no principal reuse by email).
    org2 = _org(cp, "gmap2")
    conn2 = _graph_connector(org2, _fixture(deltas=[]), trusted=False)
    _run_full(org2, conn2["id"])
    ids2, _ = dal.principal_identity_closure(
        org2, store.user_id_for_email("alice@acme.test")
    )
    assert ids2 == set()


def test_graph_delta_tombstone_revokes_visibility(cp):
    org = _org(cp, "gtomb")
    conn = _graph_connector(org, _fixture())  # delta removes doc-open @ token 5
    _run_full(org, conn["id"])
    assert "Onboarding" in _titles(
        dal.visible_heads(org, principal_identity_ids=set())
    )
    # An incremental run drives the delta removal → the org-wide doc is gone.
    dal.enqueue_run(org, conn["id"], "incremental")
    sync.process_due()
    assert "Onboarding" not in _titles(
        dal.visible_heads(org, principal_identity_ids=set())
    )


def test_graph_cross_org_isolation(cp, pg):
    org_a = _org(cp, "gorga")
    org_b = _org(cp, "gorgb")
    conn_a = _graph_connector(org_a, _fixture(deltas=[]))
    _run_full(org_a, conn_a["id"])
    # Org B has its own graph connector but org A's records never leak in.
    conn_b = _graph_connector(org_b, _fixture(items={}, deltas=[],
                                              principals={}))
    _run_full(org_b, conn_b["id"])
    assert dal.visible_heads(org_b, principal_identity_ids=set()) == []
    # Alice's principal resolves to nothing in org B (no cross-org identities).
    assert _closure_heads(org_b, "alice@acme.test") == []
    # RLS backstop: org B context cannot read org A's Graph records directly.
    with psycopg.connect(pg["app_uri"], autocommit=True) as raw:
        raw.execute("SELECT set_config('app.current_org', %s, false)",
                    (org_b,))
        assert raw.execute(
            "SELECT count(*) FROM df_source_records WHERE org_id=%s",
            (org_a,)).fetchone()[0] == 0


def test_graph_throttle_reschedules_without_advancing_cursor(cp):
    org = _org(cp, "gthr")
    conn = _graph_connector(org, _fixture(deltas=[], throttle_on_page=0))
    _run_full(org, conn["id"])
    # Nothing committed and the delta cursor never advanced (fail-safe).
    assert dal.visible_heads(org, principal_identity_ids=set()) == []
    assert dal.get_cursor(org, conn["id"]) == {}
    runs = dal.run_rows(org, conn["id"])
    assert runs and runs[0]["status"] == "failed"  # retry, not park
    assert "throttled_429" in runs[0]["last_error"]


def test_graph_no_token_persisted(cp, pg):
    org = _org(cp, "gsec")
    fx = _fixture(deltas=[])
    fx["access_token"] = "SECRET-should-never-persist"
    conn = _graph_connector(org, fx)
    _run_full(org, conn["id"])
    # The secret-shaped fixture field never reaches record versions or lineage.
    with _admin(pg) as raw:
        blob = "".join(
            str(r) for r in raw.execute(
                "SELECT title, body_ref, canonical_url, lineage_json::text "
                "FROM df_source_record_versions WHERE org_id=%s", (org,)
            ).fetchall()
        )
    assert "SECRET-should-never-persist" not in blob
