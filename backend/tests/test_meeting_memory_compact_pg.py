"""Meeting Memory Slice 3 acceptance on real Postgres as laura_app.

The slice-3 gate from docs/company-brain/MEETING-MEMORY-SPEC.md §9: cold tier
(text gone, structured facts forever), series fold (tail collapses into one
digest chunk), org cap, and 'forget this meeting' removing a meeting from
every retrieval path in one call."""
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
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("memcompact_pg")))
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
    meeting_memory._last_compaction.clear()
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


def _session(org_id, bot_id, url, names=("Dana Fox",)):
    participants = {
        f"p{i}": {"id": f"p{i}", "name": n, "kind": "human", "here": True}
        for i, n in enumerate(names)
    }
    return SimpleNamespace(
        org_id=org_id, bot_id=bot_id, meeting_url=url,
        avatar_id="laura", participants=participants,
    )


def _artifact(summary, decisions=()):
    return {
        "summary": summary, "decisions": list(decisions), "actions": [],
        "missing_steps": [], "risks": [], "readiness_score": 50,
        "meeting_type": "standup", "participation": [],
        "transcript": "SENTINEL", "avatar_id": "laura",
        "duration_seconds": 300,
    }


def test_age_cold_tier_keeps_structured_rows(mm, pg):
    org = _org("c-age")
    assert mm.deposit(
        _session(org, "bot-old", "https://zoom.us/j/1"),
        _artifact("Ancient planning session about Helios.", ["Ship Helios"]),
    )
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE memory_meetings SET ended_at = now() - interval '400 days'"
        )
    stats = mm.compact(org)
    assert stats["age_cold"] >= 1
    with _admin(pg) as conn:
        assert conn.execute(
            "SELECT count(*) FROM memory_chunks"
        ).fetchone()[0] == 0
        # The structured row survives — dates, decisions, attendees stay.
        summary, decisions = conn.execute(
            "SELECT summary, decisions_json FROM memory_meetings "
            "WHERE bot_id = 'bot-old'"
        ).fetchone()
        assert "Helios" in summary
        assert "Ship Helios" in decisions


def test_series_fold_replaces_tail_with_digest_chunk(mm, pg, monkeypatch):
    monkeypatch.setattr(settings, "meeting_memory_series_cap", 5)
    monkeypatch.setattr(settings, "meeting_memory_series_keep", 2)
    org = _org("c-series")
    for i in range(8):
        assert mm.deposit(
            _session(org, f"bot-s{i}", "https://meet.google.com/aaa-srs-aaa"),
            _artifact(f"Standup number {i} about the Pluto rollout.",
                      [f"Decision {i}"]),
        )
    stats = mm.compact(org)
    assert stats["series_folded"] == 6  # 8 - keep(2)
    with _admin(pg) as conn:
        chunked = conn.execute(
            "SELECT count(DISTINCT meeting_id) FROM memory_chunks"
        ).fetchone()[0]
        # keep(2) fully chunked + 1 digest chunk on the newest folded meeting
        assert chunked == 3
        digest = conn.execute(
            "SELECT text FROM memory_chunks WHERE text LIKE 'Series digest%'"
        ).fetchone()[0]
        assert "6 earlier meetings" in digest
        assert "Decision 0" in digest or "Standup number 0" in digest
    # The folded tail is still searchable through the digest chunk.
    room = _session(org, "bot-x", "u", ("Dana Fox",))
    out = mm.search("Pluto rollout standup", room)
    assert "Pluto" in out
    # Idempotency: a second compact pass re-derives the same fold and leaves
    # the same three chunked meetings (keep 2 + one digest carrier).
    stats2 = mm.compact(org)
    assert stats2["series_folded"] == 6
    with _admin(pg) as conn:
        assert conn.execute(
            "SELECT count(DISTINCT meeting_id) FROM memory_chunks"
        ).fetchone()[0] == 3


def test_org_cap_sends_oldest_cold(mm, pg, monkeypatch):
    monkeypatch.setattr(settings, "meeting_memory_max_docs", 3)
    org = _org("c-cap")
    for i in range(5):
        assert mm.deposit(
            _session(org, f"bot-c{i}", f"https://zoom.us/j/20{i}"),
            _artifact(f"Distinct meeting {i} about topic {i}."),
        )
        with _admin(pg) as conn:  # spread ended_at so ordering is stable
            conn.execute(
                "UPDATE memory_meetings SET ended_at = now() - interval '%s hours' "
                "WHERE bot_id = 'bot-c%s'" % (5 - i, i)
            )
    stats = mm.compact(org)
    assert stats["cap_cold"] > 0
    with _admin(pg) as conn:
        chunked = conn.execute(
            "SELECT count(DISTINCT meeting_id) FROM memory_chunks"
        ).fetchone()[0]
        assert chunked == 3
        rows = conn.execute(
            "SELECT count(*) FROM memory_meetings"
        ).fetchone()[0]
        assert rows == 5  # structured rows never deleted


def test_forget_removes_every_retrieval_path(mm, pg):
    org = _org("c-forget")
    assert mm.deposit(
        _session(org, "bot-f1", "https://zoom.us/j/301", ("Dana Fox", "Sam Lee")),
        _artifact("Sensitive meeting about the Sirius layoffs.",
                  ["Freeze Sirius hiring"]),
    )
    room = _session(org, "bot-x", "u", ("Dana Fox", "Sam Lee"))
    assert "Sirius" in mm.search("Sirius layoffs", room)
    assert mm.week_brief(org, "laura") != ""
    # A real grant exists going in, so the zero-count below actually pins
    # "forget deletes existing grants" (not vacuously).
    assert mm.grant(org, "bot-f1", "guest@corp.test")["ok"]

    res = mm.forget(org, "bot-f1")
    assert res["ok"], res
    assert "Sirius" not in mm.search("Sirius layoffs", room)
    # Digest cache was invalidated; with no other meetings the brief is empty
    # (the forgotten meeting's text no longer feeds it).
    assert "Sirius" not in mm.week_brief(org, "laura")
    with _admin(pg) as conn:
        for table in ("memory_chunks", "memory_edges", "memory_attendees",
                      "memory_grants"):
            assert conn.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0] == 0, table
        summary, mtype = conn.execute(
            "SELECT summary, meeting_type FROM memory_meetings "
            "WHERE bot_id = 'bot-f1'"
        ).fetchone()
        assert summary == "" and mtype == "forgotten"


def test_org_summary_counts(mm):
    org = _org("c-summ")
    assert mm.deposit(
        _session(org, "bot-m1", "https://zoom.us/j/401", ("Dana Fox",)),
        _artifact("Roadmap sync about Quasar.", ["Adopt Quasar"]),
    )
    res = mm.org_summary(org)
    assert res["ok"]
    assert res["meetings"] == 1 and res["meetings_with_text"] == 1
    assert res["entities"].get("person") == 1
    assert res["recent"][0]["bot_id"] == "bot-m1"
    assert "Quasar" in res["recent"][0]["summary"]


def test_maybe_compact_daily_throttle(mm):
    org = _org("c-throttle")
    assert mm.maybe_compact(org) is True
    assert mm.maybe_compact(org) is False  # second call same day: throttled
