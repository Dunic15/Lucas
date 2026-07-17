"""Org avatar overlays (M2) on real Postgres as laura_app — acceptance gates.

Full Alembic chain through 0011_org_avatars, then through the policy-bound
runtime role: two orgs personalize the same canonical avatar independently;
drafts never leak into resolution; publish is atomic and single-winner under
concurrency; rollback is append-only and traceable; RLS isolates every table;
the audit trail is INSERT-only by grant; capability narrowing re-resolves at
execution time; and a context scope filters REAL Company Brain retrieval —
with an empty restricted scope meaning "nothing", never "everything".
"""
from __future__ import annotations

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

from app import (  # noqa: E402
    avatar_resolver, avatars, control_plane, org_avatars_pg, rag, store,
)
from app.config import settings  # noqa: E402
from app.knowledge import dal as knowledge_dal  # noqa: E402
from app.knowledge import ingest  # noqa: E402
from app.knowledge import router as knowledge_router  # noqa: E402
from app import org_avatars_api  # noqa: E402

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
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("orgav_pg")))
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
    monkeypatch.setattr(settings, "org_avatar_overlays_enabled", True)
    monkeypatch.setattr(settings, "company_brain_enabled", True)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    rag._ORG_CACHE.clear()
    rag._ORG_MISS.clear()
    avatar_resolver._reset_for_tests()
    ingest._last_refresh = 0.0
    control_plane.reset_engine()
    with _admin(pg) as conn:
        for table in ("org_avatar_audit", "org_avatar_assignments",
                      "org_avatar_versions", "org_avatars",
                      "knowledge_sync_jobs", "knowledge_chunks",
                      "knowledge_document_versions", "knowledge_documents",
                      "knowledge_assignments", "knowledge_sources"):
            conn.execute(f"DELETE FROM {table}")
    yield control_plane
    rag._ORG_CACHE.clear()
    rag._ORG_MISS.clear()
    avatar_resolver._reset_for_tests()
    control_plane.reset_engine()


def _org(cp, tag: str) -> str:
    stamp = str(time.time_ns())
    return cp.ensure_user(
        f"sub-{tag}-{stamp}", f"{tag}-{stamp}@freemail.test", tag, ""
    )["org_id"]


def _publish(org: str, overlay: dict, key: str = "laura") -> int:
    org_avatars_pg.ensure_org_avatar(org, key, "test")
    draft, err = org_avatars_pg.save_draft(org, key, overlay, "test")
    assert not err, err
    version, err = org_avatars_pg.publish_draft(org, key, "test")
    assert not err, err
    avatar_resolver.invalidate(org, key)
    return version


# ── 1-3. two-org independence + canonical immutability ──────────────────────

def test_two_orgs_customize_the_same_avatar_independently(cp):
    org_a = _org(cp, "ind-a")
    org_b = _org(cp, "ind-b")
    canonical_before = avatars.load("laura").name

    _publish(org_a, {"display_name": "Ava", "enabled_tools": ["slack"]})
    _publish(org_b, {"display_name": "Bella", "tone": "formal"})

    a = avatar_resolver.resolve(org_a, "laura")
    b = avatar_resolver.resolve(org_b, "laura")
    assert a.name == "Ava" and b.name == "Bella"
    assert a.effective_tools == ("slack",)
    assert "google" in b.effective_tools  # B never narrowed tools
    # The canonical definition is untouched, and an org with NO overlay gets
    # the very same canonical instance.
    assert avatars.load("laura").name == canonical_before
    org_c = _org(cp, "ind-c")
    assert avatar_resolver.resolve(org_c, "laura") is avatars.load("laura")


# ── drafts, publish atomicity, races, rollback, immutability ────────────────

def test_unpublished_draft_never_affects_resolution(cp):
    org = _org(cp, "draft")
    org_avatars_pg.ensure_org_avatar(org, "laura", "test")
    draft, err = org_avatars_pg.save_draft(
        org, "laura", {"display_name": "DraftOnly"}, "test"
    )
    assert not err
    assert avatar_resolver.resolve(org, "laura") is avatars.load("laura")


def test_optimistic_token_blocks_stale_editors(cp):
    org = _org(cp, "token")
    org_avatars_pg.ensure_org_avatar(org, "laura", "test")
    draft, _ = org_avatars_pg.save_draft(org, "laura", {"role": "v1"}, "a")
    stale = draft["version_token"]
    fresh, _ = org_avatars_pg.save_draft(
        org, "laura", {"role": "v2"}, "b", expected_token=stale
    )
    assert fresh is not None  # token matched → rotated
    _none, err = org_avatars_pg.save_draft(
        org, "laura", {"role": "v3"}, "c", expected_token=stale
    )
    assert err == "version_conflict"


def test_concurrent_publish_single_winner(cp):
    org = _org(cp, "race")
    org_avatars_pg.ensure_org_avatar(org, "laura", "test")
    org_avatars_pg.save_draft(org, "laura", {"role": "raced"}, "test")

    def go(tag: str):
        return org_avatars_pg.publish_draft(org, "laura", tag)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(go, ["p1", "p2"]))
    oks = [r for r in results if not r[1]]
    losers = [r for r in results if r[1]]
    assert len(oks) == 1 and len(losers) == 1
    assert losers[0][1] in ("no_draft", "version_conflict")
    row = org_avatars_pg.ensure_org_avatar(org, "laura", "test")
    assert row["current_version"] == oks[0][0]


def test_published_versions_are_immutable_and_rollback_is_append_only(cp):
    org = _org(cp, "roll")
    v1 = _publish(org, {"display_name": "One"})
    v2 = _publish(org, {"display_name": "Two"})
    assert (v1, v2) == (1, 2)
    assert avatar_resolver.resolve(org, "laura").name == "Two"

    new_version, err = org_avatars_pg.publish_prior(org, "laura", v1, "test")
    assert not err and new_version == 3
    avatar_resolver.invalidate(org, "laura")
    assert avatar_resolver.resolve(org, "laura").name == "One"
    # History is linear and intact: v1 and v2 payloads unchanged.
    assert org_avatars_pg.get_version(org, "laura", v1)["overlay_json"][
        "display_name"] == "One"
    assert org_avatars_pg.get_version(org, "laura", v2)["overlay_json"][
        "display_name"] == "Two"
    versions = org_avatars_pg.list_versions(org, "laura")
    assert [v["version"] for v in versions] == [3, 2, 1]
    # Editing after publish creates a NEW draft, never touches published rows.
    org_avatars_pg.save_draft(org, "laura", {"display_name": "Four"}, "test")
    assert org_avatars_pg.get_version(org, "laura", 3)["overlay_json"][
        "display_name"] == "One"


# ── RLS + audit append-only ─────────────────────────────────────────────────

def test_rls_isolates_and_audit_is_insert_only(cp, pg):
    org_a = _org(cp, "rls-a")
    org_b = _org(cp, "rls-b")
    _publish(org_a, {"display_name": "Secret"})

    assert org_avatars_pg.current_overlay(org_b, "laura") is None
    assert org_avatars_pg.list_org_avatars(org_b) == []
    assert org_avatars_pg.list_audit(org_b) == []

    with psycopg.connect(pg["app_uri"], autocommit=True) as conn:
        conn.execute("SELECT set_config('app.current_org', %s, false)", (org_b,))
        for table in ("org_avatars", "org_avatar_versions", "org_avatar_audit"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        denied = conn.execute(
            "UPDATE org_avatars SET enabled=false WHERE org_id=%s", (org_a,)
        )
        assert denied.rowcount == 0
        # Audit is append-only BY GRANT: UPDATE/DELETE are permission errors
        # even inside one's own org.
        conn.execute("SELECT set_config('app.current_org', %s, false)", (org_a,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE org_avatar_audit SET actor='x'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM org_avatar_audit")

    actions = [a["action"] for a in org_avatars_pg.list_audit(org_a)]
    assert "draft_saved" in actions and "published" in actions


# ── assignments + precedence ────────────────────────────────────────────────

def test_assignment_precedence_is_deterministic(cp):
    org = _org(cp, "prec")
    org_avatars_pg.ensure_org_avatar(org, "laura", "t")
    org_avatars_pg.ensure_org_avatar(org, "cedric", "t")
    row, err = org_avatars_pg.upsert_assignment(
        org, "laura", "org_default", "", "t"
    )
    assert not err
    assert avatar_resolver.resolve_avatar_key(org, requested="") == "laura"
    row, err = org_avatars_pg.upsert_assignment(
        org, "cedric", "user", "u_someone", "t"
    )
    assert not err
    # user assignment beats org default; explicit request beats both.
    assert avatar_resolver.resolve_avatar_key(
        org, requested="", principal_id="u_someone"
    ) == "cedric"
    assert avatar_resolver.resolve_avatar_key(
        org, requested="petra", principal_id="u_someone"
    ) == "petra"
    # Retiring the user assignment falls back to the org default.
    assignments = org_avatars_pg.list_assignments(org)
    user_row = [a for a in assignments if a["scope_kind"] == "user"][0]
    assert org_avatars_pg.delete_assignment(org, user_row["id"], "t")
    assert avatar_resolver.resolve_avatar_key(
        org, requested="", principal_id="u_someone"
    ) == "laura"


# ── execution-time capability narrowing ─────────────────────────────────────

def test_family_allowed_recalculates_at_execution_time(cp):
    org = _org(cp, "caps")
    assert avatar_resolver.family_allowed(org, "laura", "google") is True
    _publish(org, {"enabled_tools": ["slack"]})
    assert avatar_resolver.family_allowed(org, "laura", "google") is False
    assert avatar_resolver.family_allowed(org, "laura", "slack") is True
    # Publishing a wider overlay again re-allows (recalculated, not frozen).
    _publish(org, {})
    assert avatar_resolver.family_allowed(org, "laura", "google") is True


# ── context scope filters real Company Brain retrieval ──────────────────────

def _drain_jobs(rounds: int = 8) -> None:
    for _ in range(rounds):
        if ingest.process_due() == 0:
            return


def test_context_scope_filters_real_retrieval(cp):
    org = _org(cp, "scope")
    src_a = knowledge_dal.create_source(org, "Handbook", "upload")
    src_b = knowledge_dal.create_source(org, "Pricing", "upload")
    for src, name, text in (
        (src_a, "handbook.md", "# Refunds\n\nRefunds take THREE days."),
        (src_b, "pricing.md", "# Pricing\n\nThe enterprise tier costs NINE eur."),
    ):
        code, payload = knowledge_router._upload_document(
            org, src["id"], {"filename": name, "text": text}
        )
        assert code == 200, payload
        assert knowledge_dal.assign(org, src["id"], "laura")
    _drain_jobs()

    # Unscoped resolved avatar (include_org_default): sees BOTH sources.
    _publish(org, {})
    resolved = avatar_resolver.resolve(org, "laura")
    both = " ".join(
        h.text for h in rag.retrieve(resolved, "refunds pricing", k=6,
                                     org_id=org)
    )
    assert "THREE days" in both and "NINE eur" in both

    # Restricted to the handbook only.
    _publish(org, {"context_scope": {
        "include_org_default": False,
        "knowledge_source_ids": [src_a["id"]],
    }})
    resolved = avatar_resolver.resolve(org, "laura")
    scoped = " ".join(
        h.text for h in rag.retrieve(resolved, "refunds pricing", k=6,
                                     org_id=org)
    )
    assert "THREE days" in scoped
    assert "NINE eur" not in scoped

    # Empty RESTRICTED scope = no org sources at all — never "everything".
    _publish(org, {"context_scope": {
        "include_org_default": False, "knowledge_source_ids": [],
    }})
    resolved = avatar_resolver.resolve(org, "laura")
    none = " ".join(
        h.text for h in rag.retrieve(resolved, "refunds pricing", k=6,
                                     org_id=org)
    )
    assert "THREE days" not in none and "NINE eur" not in none


def test_publish_rejects_foreign_and_unknown_sources(cp):
    org_a = _org(cp, "srcval-a")
    org_b = _org(cp, "srcval-b")
    src_b = knowledge_dal.create_source(org_b, "Theirs", "upload")
    code, payload = org_avatars_api._op_save_draft(
        org_a, "laura",
        {"overlay": {"context_scope": {
            "include_org_default": False,
            "knowledge_source_ids": [src_b["id"]],
        }}},
        "test",
    )
    assert code == 422
    assert any("not in this organization" in e for e in payload["details"])


def test_api_responses_carry_no_secret_fields(cp):
    org = _org(cp, "clean")
    _publish(org, {"display_name": "Clean"})
    code, payload = org_avatars_api._op_get(org, "laura")
    assert code == 200
    blob = str(payload).lower()
    for needle in ("refresh_token", "client_secret", "webhook_secret",
                   "api_key", "authorization"):
        assert needle not in blob
