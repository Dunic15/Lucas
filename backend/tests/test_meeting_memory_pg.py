"""Meeting Memory + pre-meeting context on real Postgres as laura_app.

The M3 acceptance matrix: the four meeting visibilities (private,
attendee-only, team-shared via group, org-visible); missing identity; stale /
revoked / deleted access; cross-org denial; the guarantee that an
inaccessible meeting contributes NO snippet and NO citation; a pre-meeting
brief that combines meeting history with Company Brain documents; and the
proof that building a context pack executes no external action.
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
from app.datafoundation import connector_meeting, dal  # noqa: E402
from app.datafoundation import meeting_memory, premeeting_context  # noqa: E402
from app.datafoundation import sync as df_sync  # noqa: E402
from app.datafoundation.envelope import body_checksum  # noqa: E402

pytestmark = pytest.mark.pg
BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_ROLE = "laura_app"

ADA = "ada@synthetic.example"
BO = "bo@synthetic.example"
CAI = "cai@synthetic.example"


def _write_extension_shims() -> None:
    ext_dir = (
        Path(pgserver.__file__).parent
        / "pginstall" / "share" / "postgresql" / "extension"
    )
    shims = {
        "pgcrypto.control": ("default_version = '1.0'\nrelocatable = true\n"
                             "comment = 'test shim'\n"),
        "pgcrypto--1.0.sql": "-- shim\n",
        "citext.control": ("default_version = '1.0'\nrelocatable = true\n"
                           "comment = 'test shim'\n"),
        "citext--1.0.sql": "CREATE DOMAIN citext AS text;\n",
    }
    for name, body in shims.items():
        path = ext_dir / name
        if not path.exists():
            path.write_text(body)


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    _write_extension_shims()
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("mm_pg")))
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
        for table in ("df_meeting_facets", "df_connectors", "df_purge_audit",
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


def _artifact(**over) -> dict:
    art = {
        "summary": "We agreed the ACMEWIDGET rollout ships in Q3.",
        "decisions": [{"text": "Ship the ACMEWIDGET pilot in Q3"}],
        "actions": [{"title": "Send the ACMEWIDGET DPA", "owner": "Ada",
                     "due": "2026-08-15"}],
        "risks": ["ACMEWIDGET security review not booked"],
        "missing_steps": ["DPA signature outstanding"],
        "meeting_url": "https://meet.google.com/acme-weekly",
        "visibility": "participants",
        "principal_id": "",
        "saved_at": time.time() - 86400.0,
        "participation": [{"name": "Ada Test"}],
        "transcript": "RAWTRANSCRIPTSENTINEL should never be indexed",
    }
    art.update(over)
    return art


def _meta(**over) -> dict:
    meta = {
        "title": "Acme weekly sync",
        "attendees": [{"name": "Ada Test", "email": ADA}],
        "customer": "Acme",
        "project": "Rollout",
        "topics": ["pricing"],
    }
    meta.update(over)
    return meta


def _index(org: str, bot_id: str, artifact: dict, meta: dict) -> None:
    assert connector_meeting.emit_finalized(
        org, bot_id, artifact, meeting_meta=meta
    )


def _identity(org: str, email: str, principal: str) -> None:
    """Bind an email identity on the meeting connector to a Laura principal —
    the mapping that turns an attendee into someone who can retrieve."""
    connector = dal.ensure_connector(org, "meeting",
                                     connector_meeting.CONNECTOR_NAME)
    dal.commit_batch(
        org, connector["id"], [],
        new_cursor=None,
        identities=[{"external_id": email, "kind": "user", "display": email}],
    )
    from sqlalchemy import text as _t

    engine = control_plane._get_engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org)
        row = conn.execute(_t(
            "SELECT id::text FROM df_identities WHERE org_id=:o "
            "AND external_id=:e"
        ), {"o": org, "e": email}).first()
    assert row is not None, f"identity {email} not mirrored"
    assert dal.bind_identity(org, str(row[0]), principal, "test")


def _titles(org: str, principal: str, query: str = "ACMEWIDGET",
            **filters) -> set[str]:
    found = meeting_memory.search(org, principal_ref=principal, query=query,
                                  filters=filters or None, k=10)
    return {r["citation"]["meeting_id"] for r in found["results"]}


# ── the four visibilities ───────────────────────────────────────────────────

def test_org_visible_meeting_is_readable_by_any_org_principal(cp):
    org = _org(cp, "mm-org")
    _index(org, "m-org", _artifact(visibility="org"), _meta())
    # No identity binding at all: an org-visible meeting still resolves.
    assert "m-org" in _titles(org, "u_someone")


def test_attendee_only_meeting_is_readable_only_by_attendees(cp):
    org = _org(cp, "mm-att")
    _index(org, "m-att", _artifact(), _meta())
    _identity(org, ADA, "u_ada")
    _identity(org, BO, "u_bo")
    assert "m-att" in _titles(org, "u_ada")      # attendee
    assert "m-att" not in _titles(org, "u_bo")   # colleague, not an attendee


def test_private_meeting_is_readable_only_by_its_dispatcher(cp):
    org = _org(cp, "mm-priv")
    _index(org, "m-priv",
           _artifact(visibility="private", principal_id="u_owner"), _meta())
    _identity(org, ADA, "u_ada")
    assert "m-priv" not in _titles(org, "u_ada")
    # The dispatcher's own principal is the ACL subject for a private meeting.
    connector = dal.ensure_connector(org, "meeting",
                                     connector_meeting.CONNECTOR_NAME)
    dal.commit_batch(org, connector["id"], [], new_cursor=None,
                     identities=[{"external_id": "u_owner", "kind": "user",
                                  "display": "owner"}])
    from sqlalchemy import text as _t

    engine = control_plane._get_engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org)
        row = conn.execute(_t(
            "SELECT id::text FROM df_identities WHERE org_id=:o "
            "AND external_id='u_owner'"
        ), {"o": org}).first()
    dal.bind_identity(org, str(row[0]), "u_owner", "test")
    assert "m-priv" in _titles(org, "u_owner")


def test_team_shared_meeting_reaches_group_members(cp):
    """A meeting shared with a team: the ACL names a group, and membership
    (including nesting) decides who reads it."""
    org = _org(cp, "mm-team")
    connector = dal.ensure_connector(org, "meeting",
                                     connector_meeting.CONNECTOR_NAME)
    fields = connector_meeting.distill(_artifact(), bot_id="m-team",
                                       meeting_meta=_meta())
    body = connector_meeting.render_body(fields)
    env = {
        "external_id": "m-team", "kind": "event", "title": "Team meeting",
        "body_text": body, "mime": "text/markdown",
        "acl_mode": "mirrored",
        "acl": [{"principal_kind": "group",
                 "principal_external_id": "g-eng", "access": "reader"}],
        "deleted": False, "checksum": body_checksum(body),
        "transform": "meeting@1",
    }
    envelopes = df_sync.materialize_bodies(org, connector, [env])
    dal.commit_batch(
        org, connector["id"], envelopes, new_cursor=None,
        identities=[
            {"external_id": "g-eng", "kind": "group", "display": "Eng",
             "members": [ADA]},
            {"external_id": ADA, "kind": "user", "display": "Ada"},
            {"external_id": BO, "kind": "user", "display": "Bo"},
        ],
    )
    dal.set_connector_acl_mirrored(org, connector["id"], True)
    _bind(org, ADA, "u_ada")
    _bind(org, BO, "u_bo")
    assert "m-team" in _titles(org, "u_ada")   # in the group
    assert "m-team" not in _titles(org, "u_bo")  # not in the group


def _bind(org: str, external_id: str, principal: str) -> None:
    from sqlalchemy import text as _t

    engine = control_plane._get_engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org)
        row = conn.execute(_t(
            "SELECT id::text FROM df_identities WHERE org_id=:o "
            "AND external_id=:e"
        ), {"o": org, "e": external_id}).first()
    assert row is not None
    assert dal.bind_identity(org, str(row[0]), principal, "test")


# ── denial paths ────────────────────────────────────────────────────────────

def test_missing_identity_defaults_to_deny(cp):
    org = _org(cp, "mm-noid")
    _index(org, "m-noid", _artifact(), _meta())
    # Attendee exists in the ACL but is bound to no Laura principal.
    assert _titles(org, "u_unmapped") == set()
    assert _titles(org, "") == set()


def test_unresolvable_attendees_make_the_meeting_visible_to_nobody(cp):
    """Fail-closed: a participants-only meeting whose attendees carry no
    stable identity is stored with acl_mode='unknown' — nobody, not everybody."""
    org = _org(cp, "mm-unknown")
    _index(org, "m-unknown",
           _artifact(principal_id="", participation=[{"name": "Someone"}]),
           _meta(attendees=[], title="No identities"))
    _identity(org, ADA, "u_ada")
    assert _titles(org, "u_ada") == set()
    assert _titles(org, "u_anyone") == set()


def test_revoked_connector_hides_meetings_immediately(cp):
    org = _org(cp, "mm-revoke")
    _index(org, "m-rev", _artifact(), _meta())
    _identity(org, ADA, "u_ada")
    assert "m-rev" in _titles(org, "u_ada")
    connector = dal.ensure_connector(org, "meeting",
                                     connector_meeting.CONNECTOR_NAME)
    dal.set_connector_status(org, connector["id"], "needs_reconnect",
                             actor="test")
    assert _titles(org, "u_ada") == set()


def test_deleted_meeting_is_tombstoned_out_of_retrieval(cp):
    org = _org(cp, "mm-del")
    _index(org, "m-del", _artifact(), _meta())
    _identity(org, ADA, "u_ada")
    assert "m-del" in _titles(org, "u_ada")
    assert connector_meeting.emit_finalized(org, "m-del", _artifact(),
                                            meeting_meta=_meta(), deleted=True)
    assert _titles(org, "u_ada") == set()


def test_acl_revocation_by_reindex_removes_access(cp):
    """The attendee list shrinks on re-finalize: the removed person loses
    access on the next index, not at some cache expiry."""
    org = _org(cp, "mm-aclchange")
    meta_both = _meta(attendees=[{"name": "Ada", "email": ADA},
                                 {"name": "Bo", "email": BO}])
    _index(org, "m-acl", _artifact(), meta_both)
    _identity(org, ADA, "u_ada")
    _identity(org, BO, "u_bo")
    assert "m-acl" in _titles(org, "u_bo")
    _index(org, "m-acl", _artifact(), _meta())  # Bo no longer an attendee
    assert "m-acl" not in _titles(org, "u_bo")
    assert "m-acl" in _titles(org, "u_ada")


def test_cross_org_meetings_never_leak(cp):
    org_a = _org(cp, "mm-a")
    org_b = _org(cp, "mm-b")
    _index(org_a, "m-a", _artifact(visibility="org"), _meta())
    _index(org_b, "m-b",
           _artifact(visibility="org",
                     summary="ORGBSENTINEL private to org B"),
           _meta(title="Org B meeting"))
    assert _titles(org_a, "u_x", query="ORGBSENTINEL") == set()
    assert _titles(org_b, "u_x", query="ORGBSENTINEL") == {"m-b"}
    assert _titles(org_a, "u_x", query="ACMEWIDGET") == {"m-a"}


def test_inaccessible_meeting_contributes_no_snippet_or_citation(cp):
    """The strongest form of the rule: not a snippet, not a citation, not a
    count — an unauthorized meeting is simply absent."""
    org = _org(cp, "mm-nosnip")
    _index(org, "m-secret",
           _artifact(summary="TOPSECRETSENTINEL merger terms"), _meta())
    _identity(org, ADA, "u_ada")
    _identity(org, BO, "u_bo")
    found = meeting_memory.search(org, principal_ref="u_bo",
                                  query="TOPSECRETSENTINEL", k=10)
    assert found["results"] == []
    assert found["meetings"] == 0
    blob = repr(found)
    assert "TOPSECRETSENTINEL" not in blob
    assert "m-secret" not in blob


def test_transcript_is_never_indexed(cp):
    org = _org(cp, "mm-pii")
    _index(org, "m-pii", _artifact(), _meta())
    _identity(org, ADA, "u_ada")
    found = meeting_memory.search(org, principal_ref="u_ada",
                                  query="RAWTRANSCRIPTSENTINEL", k=10)
    assert found["results"] == []
    hits = meeting_memory.search(org, principal_ref="u_ada",
                                 query="ACMEWIDGET", k=10)["results"]
    assert hits
    assert all("RAWTRANSCRIPTSENTINEL" not in h["excerpt"] for h in hits)


# ── filters + citations ─────────────────────────────────────────────────────

def test_filters_narrow_by_customer_project_participant_and_date(cp):
    org = _org(cp, "mm-filter")
    now = time.time()
    _index(org, "m-acme",
           _artifact(visibility="org", saved_at=now - 10 * 86400.0), _meta())
    _index(org, "m-globex",
           _artifact(visibility="org", saved_at=now - 10 * 86400.0),
           _meta(title="Globex sync", customer="Globex", project="Migration",
                 attendees=[{"name": "Cai", "email": CAI}]))
    _index(org, "m-old",
           _artifact(visibility="org", saved_at=now - 400 * 86400.0),
           _meta(title="Ancient Acme call"))

    assert _titles(org, "u_x", customer="Acme") == {"m-acme", "m-old"}
    assert _titles(org, "u_x", project="Migration") == {"m-globex"}
    assert _titles(org, "u_x", participant="cai@") == {"m-globex"}
    assert _titles(org, "u_x", topic="pricing") >= {"m-acme"}
    recent = _titles(org, "u_x", customer="Acme", since=now - 180 * 86400.0)
    assert recent == {"m-acme"}  # the six-month question


def test_citations_carry_title_date_and_meeting_id(cp):
    org = _org(cp, "mm-cite")
    _index(org, "m-cite", _artifact(visibility="org"), _meta())
    hit = meeting_memory.search(org, principal_ref="u_x", query="ACMEWIDGET",
                                k=5)["results"][0]
    cite = hit["citation"]
    assert cite["meeting_id"] == "m-cite"
    assert cite["title"] == "Acme weekly sync"
    assert isinstance(cite["date"], float) and cite["date"] > 0
    assert cite["customer"] == "Acme"
    assert "ACMEWIDGET" in hit["excerpt"]


def test_reindexing_the_same_meeting_is_idempotent(cp, pg):
    org = _org(cp, "mm-idem")
    for _ in range(3):
        _index(org, "m-same", _artifact(visibility="org"), _meta())
    with _admin(pg) as conn:
        records = conn.execute(
            "SELECT COUNT(*) FROM df_source_records WHERE external_id='m-same'"
        ).fetchone()[0]
        facets = conn.execute(
            "SELECT COUNT(*) FROM df_meeting_facets WHERE meeting_id='m-same'"
        ).fetchone()[0]
    assert records == 1 and facets == 1
    assert _titles(org, "u_x") == {"m-same"}


# ── pre-meeting context ─────────────────────────────────────────────────────

def test_premeeting_pack_combines_history_and_company_documents(cp):
    org = _org(cp, "mm-pack")
    _index(org, "m-prev", _artifact(visibility="org"), _meta())
    # A Company Brain document the same principal may read (org_default).
    doc_connector = dal.ensure_connector(org, "upload", "Docs")
    body = ("# ACMEWIDGET pricing policy\n\nEnterprise discounts require "
            "finance approval.\n")
    env = {"external_id": "doc-policy", "kind": "document",
           "title": "Pricing policy", "body_text": body,
           "mime": "text/markdown", "acl_mode": "org_default", "acl": [],
           "deleted": False, "checksum": body_checksum(body),
           "transform": "test@1"}
    dal.commit_batch(org, doc_connector["id"],
                     df_sync.materialize_bodies(org, doc_connector, [env]),
                     new_cursor=None)

    pack = premeeting_context.build(
        org, principal_ref="u_x",
        event={"title": "Acme weekly sync", "agenda": "ACMEWIDGET rollout",
               "customer": "Acme", "attendees": [{"name": "Ada",
                                                  "email": ADA}]},
    )
    assert pack["read_only"] is True
    assert pack["relationship"]["prior_meetings"] == 1
    assert pack["previously"][0]["meeting_id"] == "m-prev"
    assert any(c["kind"] == "meeting" for c in pack["citations"])
    assert any(c["kind"] == "document" for c in pack["citations"])
    assert pack["open_commitments"], "the open DPA action should surface"
    assert any("DPA" in c["title"] for c in pack["open_commitments"])
    assert pack["risks_and_open_questions"]
    assert pack["discussion_points"]
    assert pack["freshness"]["generated_at"] > 0


def test_premeeting_pack_excludes_meetings_the_requester_cannot_read(cp):
    org = _org(cp, "mm-packacl")
    _index(org, "m-hidden",
           _artifact(summary="HIDDENSENTINEL pricing"), _meta())
    _identity(org, ADA, "u_ada")
    _identity(org, BO, "u_bo")
    pack = premeeting_context.build(
        org, principal_ref="u_bo",
        event={"title": "Acme weekly sync", "customer": "Acme",
               "attendees": [{"name": "Bo", "email": BO}]},
    )
    assert pack["previously"] == []
    assert pack["citations"] == []
    assert "HIDDENSENTINEL" not in repr(pack)


def test_building_context_executes_no_external_action(cp, monkeypatch):
    """The pack is read-only: no executor call, no queued action, no outbox
    delivery may happen while assembling it."""
    org = _org(cp, "mm-noact")
    _index(org, "m-act", _artifact(visibility="org"), _meta())

    fired: list[str] = []
    from app.actions import executor, outbox

    monkeypatch.setattr(executor, "execute_approved",
                        lambda *a, **k: fired.append("execute") or {})
    monkeypatch.setattr(outbox, "process_due",
                        lambda *a, **k: fired.append("outbox") or 0)
    for name in ("enqueue_session_ended", "checkpoint_session_ended"):
        if hasattr(outbox, name):
            monkeypatch.setattr(outbox, name,
                                lambda *a, **k: fired.append(name))

    pack = premeeting_context.build(
        org, principal_ref="u_x",
        event={"title": "Acme weekly sync", "customer": "Acme"},
    )
    assert pack["previously"], "sanity: the pack actually did work"
    assert fired == [], f"context build performed an action: {fired}"


def test_pack_degrades_closed_when_data_foundation_is_off(cp, monkeypatch):
    org = _org(cp, "mm-off")
    _index(org, "m-off", _artifact(visibility="org"), _meta())
    monkeypatch.setattr(settings, "data_foundation_enabled", False)
    pack = premeeting_context.build(
        org, principal_ref="u_x", event={"title": "Acme weekly sync"},
    )
    assert pack["previously"] == [] and pack["citations"] == []
    assert pack["freshness"]["degraded"] is True
    assert pack["freshness"]["reason"] == "data_foundation_disabled"
