"""Meeting Memory Slice 1 acceptance on real Postgres as laura_app.

The slice-1 gate from docs/company-brain/MEETING-MEMORY-SPEC.md: two meetings
on DIFFERENT links accumulate into one org's week brief; the digest is
TTL-cached (one generation per window, not one per join); no transcript text
ever reaches a memory row; person entities dedupe by construction; re-deposit
is idempotent; and nothing crosses org_id under FORCE RLS."""
from __future__ import annotations

import json
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

SENTINEL = "UTTERANCE-SENTINEL-NEVER-STORED"
_MEMORY_TABLES = (
    "memory_attendees",
    "memory_digests",
    "memory_meetings",
    "memory_entities",
)


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
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("memory_pg")))
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
    yield {"uri": uri, "app_sa_url": app_sa_url}
    control_plane.reset_engine()
    srv.cleanup()


def _admin(pg):
    return psycopg.connect(pg["uri"], autocommit=True)


@pytest.fixture
def mm(pg, monkeypatch):
    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    monkeypatch.setattr(settings, "meeting_memory_enabled", True)
    # Force the deterministic digest path — no model call in tests.
    monkeypatch.setattr(settings, "brain_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    control_plane.reset_engine()
    with _admin(pg) as conn:
        for table in _MEMORY_TABLES:
            conn.execute(f"DELETE FROM {table}")
    yield meeting_memory
    control_plane.reset_engine()


def _org(tag: str) -> str:
    stamp = str(time.time_ns())
    return control_plane.ensure_user(
        f"sub-{tag}-{stamp}", f"{tag}-{stamp}@freemail.test", tag, ""
    )["org_id"]


def _session(org_id: str, bot_id: str, url: str, names=("Dana Fox", "Sam Lee")):
    participants = {
        f"p{i}": {"id": f"p{i}", "name": n, "kind": "human", "here": True}
        for i, n in enumerate(names)
    }
    participants["agent"] = {
        "id": "agent", "name": "Laura", "kind": "agent", "here": True
    }
    return SimpleNamespace(
        org_id=org_id,
        bot_id=bot_id,
        meeting_url=url,
        avatar_id="laura",
        participants=participants,
    )


def _artifact(summary: str, decisions=(), transcript: str = "") -> dict:
    return {
        "summary": summary,
        "decisions": list(decisions),
        "actions": [
            {
                "item": "Send the DPA",
                "owner": "Dana",
                "deadline": "Friday",
                "action_id": "a1",
                "gap_type": "none",
                "evidence": f"Dana: {SENTINEL} I'll handle it",
            }
        ],
        "missing_steps": ["risk review"],
        "risks": [],
        "readiness_score": 80,
        "meeting_type": "standup",
        "participation": [{"name": "Dana Fox", "lines": 10}],
        "transcript": transcript or f"Dana: {SENTINEL}\nSam: more {SENTINEL}",
        "avatar_id": "laura",
        "duration_seconds": 900,
    }


def test_two_links_accumulate_and_digest_is_cached(mm, pg):
    org = _org("mem-a")
    assert mm.deposit(
        _session(org, "bot-a1", "https://meet.google.com/aaa-aaaa-aaa"),
        _artifact("Reviewed the Q3 launch plan.", ["Ship on Aug 15"]),
    )
    assert mm.deposit(
        _session(org, "bot-a2", "https://zoom.us/j/123456789"),
        _artifact("Customer call about onboarding delays.", ["Add a CSM"]),
    )

    brief = mm.week_brief(org, "laura")
    # Cross-LINK accumulation — the whole point of the slice.
    assert "Q3 launch plan" in brief
    assert "onboarding delays" in brief
    assert len(brief) <= settings.meeting_memory_digest_max_chars

    # TTL cache: a third meeting inside the window does NOT appear until the
    # digest expires (one generation per window, not one per join).
    assert mm.deposit(
        _session(org, "bot-a3", "https://meet.google.com/bbb-bbbb-bbb"),
        _artifact("Budget sync happened."),
    )
    assert "Budget sync" not in mm.week_brief(org, "laura")
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE memory_digests SET generated_at = now() - interval '2 hours'"
        )
    assert "Budget sync" in mm.week_brief(org, "laura")


def test_no_transcript_text_in_any_memory_row(mm, pg):
    org = _org("mem-pii")
    assert mm.deposit(
        _session(org, "bot-pii", "https://meet.google.com/ccc-cccc-ccc"),
        _artifact("Clean distilled summary.", ["A decision"]),
    )
    mm.week_brief(org, "laura")
    with _admin(pg) as conn:
        for table in _MEMORY_TABLES:
            cols = [
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s AND data_type = 'text'",
                    (table,),
                )
            ]
            for col in cols:
                hits = conn.execute(
                    f"SELECT count(*) FROM {table} "  # noqa: S608 — test, names from schema
                    f"WHERE {col} LIKE %s",
                    (f"%{SENTINEL}%",),
                ).fetchone()[0]
                assert hits == 0, f"transcript text leaked into {table}.{col}"


def test_redeposit_is_idempotent_and_entities_dedupe(mm, pg):
    org = _org("mem-dupe")
    session = _session(org, "bot-d1", "https://meet.google.com/ddd-dddd-ddd")
    assert mm.deposit(session, _artifact("First pass."))
    assert mm.deposit(session, _artifact("Second pass, corrected."))
    assert mm.deposit(
        _session(org, "bot-d2", "https://zoom.us/j/999", names=("dana  FOX",)),
        _artifact("Different meeting, same human."),
    )
    with _admin(pg) as conn:
        n_meetings = conn.execute(
            "SELECT count(*) FROM memory_meetings WHERE bot_id = 'bot-d1'"
        ).fetchone()[0]
        assert n_meetings == 1
        summary = conn.execute(
            "SELECT summary FROM memory_meetings WHERE bot_id = 'bot-d1'"
        ).fetchone()[0]
        assert summary == "Second pass, corrected."
        # "Dana Fox" / "dana  FOX" collapse to ONE person node (dn: key).
        n_dana = conn.execute(
            "SELECT count(*) FROM memory_entities "
            "WHERE kind = 'person' AND key = 'dn:dana fox'"
        ).fetchone()[0]
        assert n_dana == 1
        mentions = conn.execute(
            "SELECT mention_count FROM memory_entities "
            "WHERE kind = 'person' AND key = 'dn:dana fox'"
        ).fetchone()[0]
        assert mentions >= 3


def test_cross_org_isolation_under_rls(mm):
    org_a = _org("mem-iso-a")
    org_b = _org("mem-iso-b")
    assert mm.deposit(
        _session(org_a, "bot-i1", "https://meet.google.com/eee-eeee-eee"),
        _artifact("Org A confidential plan.", ["Acquire Beta Corp"]),
    )
    assert mm.week_brief(org_a, "laura") != ""
    # Org B sees nothing of org A — no meetings, no digest, empty brief.
    assert mm.week_brief(org_b, "laura") == ""
    assert mm.cached_digest(org_b, "laura") == ""


def test_cached_digest_is_stale_ok_and_readonly(mm, pg):
    org = _org("mem-stale")
    assert mm.deposit(
        _session(org, "bot-s1", "https://meet.google.com/fff-ffff-fff"),
        _artifact("Weekly planning notes."),
    )
    assert "Weekly planning" in mm.week_brief(org, "laura")
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE memory_digests SET generated_at = now() - interval '9 days'"
        )
    # week_brief would regenerate; the live-path read serves the stale row
    # as-is (never a model call, never a write).
    assert "Weekly planning" in mm.cached_digest(org, "laura")
    with _admin(pg) as conn:
        age = conn.execute(
            "SELECT count(*) FROM memory_digests "
            "WHERE generated_at < now() - interval '8 days'"
        ).fetchone()[0]
        assert age == 1  # cached_digest did not refresh the row


def test_non_durable_org_and_disabled_paths_stay_empty(mm, monkeypatch):
    assert mm.deposit(
        _session("u_abc123", "bot-p1", "https://meet.google.com/ggg-gggg-ggg"),
        _artifact("Personal-org meeting."),
    ) is False
    assert mm.week_brief("u_abc123", "laura") == ""
    monkeypatch.setattr(settings, "meeting_memory_enabled", False)
    org = _org("mem-off")
    assert mm.deposit(
        _session(org, "bot-p2", "https://meet.google.com/hhh-hhhh-hhh"),
        _artifact("Flag went off."),
    ) is False
    assert mm.week_brief(org, "laura") == ""


def test_digest_hard_cap(mm, monkeypatch):
    org = _org("mem-cap")
    for i in range(12):
        assert mm.deposit(
            _session(org, f"bot-c{i}", f"https://zoom.us/j/1000{i}"),
            _artifact(("Very long recurring summary " + "z" * 300)[:400]),
        )
    monkeypatch.setattr(settings, "meeting_memory_digest_max_chars", 500)
    brief = mm.week_brief(org, "laura")
    assert brief != ""
    assert len(brief) <= 500
