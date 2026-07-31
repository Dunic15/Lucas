"""Meeting Memory people/email harvest (post-live-test fixes) on real
Postgres as laura_app: calendar-contact email enrichment at deposit,
unambiguous-only name matching, dn→email node merge, and person_lookup."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import control_plane  # noqa: E402
from app.config import settings  # noqa: E402
from app.memory import meeting_memory  # noqa: E402

pytestmark = pytest.mark.pg
BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_ROLE = "laura_app"

_TABLES_DELETE_ORDER = (
    "memory_attendees", "memory_chunks", "memory_edges", "memory_grants",
    "memory_digests", "memory_meetings", "memory_entities",
)


def _write_extension_shims() -> None:
    ext_dir = (
        Path(pgserver.__file__).parent
        / "pginstall" / "share" / "postgresql" / "extension"
    )
    shims = {
        "pgcrypto.control": (
            "default_version = '1.0'\nrelocatable = true\ncomment = 'shim'\n"
        ),
        "pgcrypto--1.0.sql": "-- shim\n",
        "citext.control": (
            "default_version = '1.0'\nrelocatable = true\ncomment = 'shim'\n"
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
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("mempeople_pg")))
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
        k: str(info[k]) for k in ("host", "port") if info.get(k) is not None
    }
    app_sa_url = URL.create(
        "postgresql+psycopg", username=APP_ROLE, password="pw",
        database=info.get("dbname"), query=query,
    ).render_as_string(hide_password=False)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env={**os.environ, "LAURA_DATABASE_ADMIN_URL": admin_sa_url,
             "LAURA_DATABASE_URL": "", "LAURA_REQUIRE_MIGRATIONS": "1"},
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    yield {"uri": uri, "app_sa_url": app_sa_url}
    control_plane.reset_engine()
    srv.cleanup()


def _admin(pg):
    return psycopg.connect(pg["uri"], autocommit=True)


@pytest.fixture
def mm(pg, monkeypatch):
    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    monkeypatch.setattr(settings, "meeting_memory_enabled", True)
    monkeypatch.setattr(settings, "brain_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "embedding_provider", "hash")
    monkeypatch.setattr(settings, "meeting_memory_trust_display_names", True)
    control_plane.reset_engine()
    with _admin(pg) as conn:
        for table in _TABLES_DELETE_ORDER:
            conn.execute(f"DELETE FROM {table}")
    yield meeting_memory
    control_plane.reset_engine()


def _org(tag: str) -> str:
    stamp = str(time.time_ns())
    return control_plane.ensure_user(
        f"sub-{tag}-{stamp}", f"{tag}-{stamp}@freemail.test", tag, ""
    )["org_id"]


def _session(org_id, bot_id, url, names, calendar_people=()):
    participants = {
        f"p{i}": {"id": f"p{i}", "name": n, "kind": "human", "here": True}
        for i, n in enumerate(names)
    }
    return SimpleNamespace(
        org_id=org_id, bot_id=bot_id, meeting_url=url,
        avatar_id="laura", participants=participants,
        calendar_people=list(calendar_people),
    )


def _artifact(summary, decisions=()):
    return {
        "summary": summary, "decisions": list(decisions), "actions": [],
        "missing_steps": [], "risks": [], "readiness_score": 50,
        "meeting_type": "standup", "participation": [],
        "transcript": "SENTINEL", "avatar_id": "laura",
        "duration_seconds": 300,
    }


def test_calendar_people_enrich_deposit_with_email(mm, pg):
    org = _org("p-enrich")
    url = "https://meet.google.com/aaa-pepl-aaa"
    from app.actions import ledger

    mk = ledger.meeting_key(url)
    session = _session(
        org, "bot-p1", url, ("Duccio Rossi", "Ananth Iyer"),
        calendar_people=[
            {"email": "duccio@sffstudio.test", "name": "Duccio Rossi",
             "meeting_key": mk},
            {"email": "ananth@sffstudio.test", "name": "Ananth Iyer",
             "meeting_key": ""},
        ],
    )
    assert mm.deposit(session, _artifact("Weekly sync about the Nimbus plan."))
    with _admin(pg) as conn:
        rows = dict(
            conn.execute(
                "SELECT key, email FROM memory_entities WHERE kind='person'"
            ).fetchall()
        )
        # Email-keyed nodes (scoped match AND org-wide unambiguous match).
        assert rows.get("duccio@sffstudio.test") == "duccio@sffstudio.test"
        assert rows.get("ananth@sffstudio.test") == "ananth@sffstudio.test"
        res = conn.execute(
            "SELECT DISTINCT resolution FROM memory_attendees"
        ).fetchall()
        assert {r[0] for r in res} == {"directory"}


def test_ambiguous_name_gets_no_email(mm, pg):
    org = _org("p-ambig")
    session = _session(
        org, "bot-p2", "https://zoom.us/j/501", ("Sam Lee",),
        calendar_people=[
            {"email": "sam.a@corp.test", "name": "Sam Lee", "meeting_key": ""},
            {"email": "sam.b@corp.test", "name": "Sam Lee", "meeting_key": ""},
        ],
    )
    assert mm.deposit(session, _artifact("Ambiguity check meeting."))
    with _admin(pg) as conn:
        key, email = conn.execute(
            "SELECT key, email FROM memory_entities WHERE kind='person'"
        ).fetchone()
        assert key == "dn:sam lee"
        assert email == ""


def test_dn_node_merges_into_email_node(mm, pg):
    org = _org("p-merge")
    # Meeting 1: no calendar data — Dana becomes a dn: node with edges.
    assert mm.deposit(
        _session(org, "bot-m1", "https://zoom.us/j/601", ("Dana Fox",)),
        _artifact("Early planning.", ["Adopt the Vega stack"]),
    )
    # Meeting 2: calendar knows Dana's email — nodes must merge.
    assert mm.deposit(
        _session(
            org, "bot-m2", "https://zoom.us/j/602", ("Dana Fox",),
            calendar_people=[
                {"email": "dana@corp.test", "name": "Dana Fox",
                 "meeting_key": ""}
            ],
        ),
        _artifact("Follow-up planning."),
    )
    with _admin(pg) as conn:
        rows = conn.execute(
            "SELECT key, email, alias_of IS NOT NULL AS aliased, mention_count "
            "FROM memory_entities WHERE kind='person' ORDER BY key"
        ).fetchall()
        by_key = {r[0]: r for r in rows}
        assert by_key["dana@corp.test"][2] is False
        assert by_key["dn:dana fox"][2] is True  # merged away
        canon_id = conn.execute(
            "SELECT id FROM memory_entities WHERE key='dana@corp.test'"
        ).fetchone()[0]
        # Both meetings' attendance now hangs off the canonical node.
        n_att = conn.execute(
            "SELECT count(*) FROM memory_attendees WHERE entity_id = %s",
            (canon_id,),
        ).fetchone()[0]
        assert n_att == 2
        # No attendance or edges left on the aliased node.
        old_id = conn.execute(
            "SELECT id FROM memory_entities WHERE key='dn:dana fox'"
        ).fetchone()[0]
        for table, col in (("memory_attendees", "entity_id"),
                           ("memory_edges", "src_id"),
                           ("memory_edges", "dst_id")):
            assert conn.execute(
                f"SELECT count(*) FROM {table} WHERE {col} = %s", (old_id,)
            ).fetchone()[0] == 0, (table, col)


def test_person_lookup_finds_email_and_misses_honestly(mm):
    org = _org("p-lookup")
    assert mm.deposit(
        _session(
            org, "bot-l1", "https://zoom.us/j/701", ("Duccio Rossi",),
            calendar_people=[
                {"email": "duccio@sffstudio.test", "name": "Duccio Rossi",
                 "meeting_key": ""}
            ],
        ),
        _artifact("Investor prep."),
    )
    room = _session(org, "bot-x", "u", ("Ananth Iyer",))
    out = mm.lookup_person("duccio", room)
    assert "duccio@sffstudio.test" in out
    assert "remembered meeting" in out
    out = mm.lookup_person("nobody whatsoever", room)
    assert "no one matching" in out


def test_projects_and_person_profile(mm, pg):
    org = _org("p-profile")
    art = _artifact(
        "Atlas migration planning with Dana leading.",
        decisions=["Migrate Atlas on Sept 1"],
    )
    art["decision_records"] = [
        {"decision": "Migrate Atlas on Sept 1", "decision_maker": "Dana Fox",
         "reason": "contract renewal", "related_project": "Atlas Migration!"}
    ]
    art["actions"] = [
        {"item": "Draft the Atlas runbook", "owner": "Dana Fox",
         "action_id": "act-p1", "gap_type": "none", "evidence": "x"}
    ]
    assert mm.deposit(
        _session(org, "bot-pr1", "https://zoom.us/j/801",
                 ("Dana Fox", "Sam Lee")),
        art,
    )
    # Same project as "Atlas-Migration" (hyphen) and "Atlas Migration!"
    # (punctuation) ⇒ SAME node under punctuation→space normalization.
    art2 = _artifact("Atlas follow-up.")
    art2["decision_records"] = [
        {"decision": "Keep the Atlas cutover date", "decision_maker": "",
         "reason": "", "related_project": "Atlas-Migration"}
    ]
    assert mm.deposit(
        _session(org, "bot-pr2", "https://zoom.us/j/802",
                 ("Dana Fox", "Sam Lee")),
        art2,
    )
    with _admin(pg) as conn:
        n_proj = conn.execute(
            "SELECT count(*) FROM memory_entities WHERE kind='project'"
        ).fetchone()[0]
        assert n_proj == 1  # exact-normalized dedupe
        rels = dict(
            conn.execute(
                "SELECT rel, count(*) FROM memory_edges GROUP BY rel"
            ).fetchall()
        )
        assert rels.get("about", 0) >= 3  # 2 meeting-about + decision-about
        # decision_maker matched an attendee → person decided edge.
        assert rels.get("decided", 0) >= 3  # 2 meeting-decided + 1 person

    room = _session(org, "bot-x", "u", ("Sam Lee",))
    out = mm.lookup_person("dana", room)
    assert "projects: Atlas-Migration" in out  # latest display, ONE node
    assert "recent meetings:" in out
    assert "owns actions: Draft the Atlas runbook" in out
    assert "often meets: Sam Lee (2×)" in out


def test_lookup_uses_session_calendar_people_without_memory(mm):
    org = _org("p-cal-only")
    room = _session(
        org, "bot-x", "u", ("Ananth Iyer",),
        calendar_people=[
            {"email": "maria@corp.test", "name": "Maria Cuelliga",
             "meeting_key": ""}
        ],
    )
    out = mm.lookup_person("maria", room)
    assert "maria@corp.test" in out
    assert "on the calendar" in out
