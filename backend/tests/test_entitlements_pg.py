"""PR B — usage metering + the 15-minute free entitlement, on REAL Postgres.

Boots an embedded Postgres (pgserver), runs ``alembic upgrade head`` (0001 +
0002 + 0003 usage spine), then proves the money-shaped invariants:

- a first signup gets 900 included seconds; the remaining-math counts closed
  consumption AND the live elapsed of an active meeting (pending rows: 0);
- 10 min of Laura + 5 min of Cedric exhaust ONE shared 900s allowance, and
  the /sessions/start route answers the EXACT 402 body without dispatching;
- a failed join consumes zero; a silent meeting consumes wall-clock (the
  clock is status-based, transcripts play no part);
- two racing starts cannot overspend (the FOR UPDATE gate + the partial
  unique index decide at the database);
- the reconcile pass stops the avatar at the deadline and PRESERVES the
  artifact (the hardened finalize path), closing usage 'limit_reached';
- a redeploy-wiped local store does NOT wipe enforcement: the durable usage
  row alone is enough to stop the meter (restart restore);
- a billing-DB outage fails CLOSED (503 before any vendor dispatch);
- with the control plane disabled the key-free demo is byte-identical
  (no gate, no engine, no new status codes).

Skipped when pgserver isn't installed (CI installs it; the key-free suite is
otherwise untouched). Extension shims: see test_control_plane_pg.py.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import control_plane, entitlements, ledger, main, store  # noqa: E402
from app.api import sessions as _sessions  # noqa: E402  (sessions extracted)
from app.meeting import lifecycle  # noqa: E402  (lifecycle hoisted from main)
from app.config import settings  # noqa: E402

pytestmark = pytest.mark.pg

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_ROLE = "laura_app"
_MEET_URL = "https://meet.google.com/abc-defg-hij"


def _write_extension_shims() -> None:
    ext_dir = (
        Path(pgserver.__file__).parent
        / "pginstall"
        / "share"
        / "postgresql"
        / "extension"
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
    """Embedded Postgres with the FULL migration chain applied (0001–0003)."""
    _write_extension_shims()
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("ent_pg")))
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(
            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'pw' NOSUPERUSER NOBYPASSRLS"
        )
    admin_sa_url = uri.replace("postgresql://", "postgresql+psycopg://")

    import psycopg.conninfo as _ci
    from sqlalchemy.engine import URL

    info = _ci.conninfo_to_dict(uri)
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
    assert proc.returncode == 0, f"alembic upgrade failed:\n{proc.stdout}\n{proc.stderr}"
    yield {"uri": uri, "admin_sa_url": admin_sa_url, "app_sa_url": app_sa_url}
    control_plane.reset_engine()
    srv.cleanup()


def _admin(pg):
    return psycopg.connect(pg["uri"], autocommit=True)


@pytest.fixture
def cp(pg, monkeypatch):
    """Control plane + entitlements pointed at the embedded PG, with a CLEAN
    usage table per test (rows persist across the module-scoped server, and a
    leftover pending/active row would trip the one-per-org unique index)."""
    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    control_plane.reset_engine()
    with _admin(pg) as conn:  # superuser: bypasses RLS by attribute
        conn.execute("DELETE FROM usage_sessions")
    yield control_plane
    control_plane.reset_engine()


def _org(cp, tag: str) -> str:
    """A fresh personal org (unique email per tag)."""
    return cp.ensure_user(f"sub-{tag}", f"{tag}@freemail.test", tag, "")["org_id"]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class _FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200):
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _bot_json(bot_id: str, changes: list[tuple[str, float]]) -> dict:
    return {
        "id": bot_id,
        "status_changes": [
            {"code": code, "created_at": _iso(at)} for code, at in changes
        ],
    }


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Clean, isolated sqlite store + no leftover in-flight/miss state."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    main._finalizing.clear()
    main._reconcile_missing.clear()
    yield
    main._finalizing.clear()
    main._reconcile_missing.clear()


@pytest.fixture
def start_env(fresh_store, monkeypatch):
    """Stub Recall/ledger/drive for the manual-start path (mirrors
    test_meter_safety); returns the list of create_bot calls."""
    created: list[str] = []

    def fake_create_bot(meeting_url, avatar_page_url, join_at=None, bot_name="Laura", avatar_id=""):
        created.append(meeting_url)
        return {"id": f"bot_{len(created)}"}

    monkeypatch.setattr(main.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(main.recall_client, "create_bot", fake_create_bot)
    monkeypatch.setattr(main.ledger, "carryover_brief", lambda url, **kw: "")
    monkeypatch.setattr(main.drive_client, "folder_brief", lambda fid: "")
    monkeypatch.setattr(_sessions, "_schedule_start_reconcile", lambda *a, **k: None)
    monkeypatch.setattr(main, "_meeting_has_active_bot", lambda url: False)
    return created


def _stub_finalize_vendors(monkeypatch):
    monkeypatch.setattr(main.recall_client, "leave_call", lambda b: None)
    monkeypatch.setattr(main.anam_client, "end_conversation", lambda c: None)
    monkeypatch.setattr(
        lifecycle, "post_meeting",
        lambda avatar, transcript, **kw: {"summary": "s", "actions": [], "checklist": []},
    )
    monkeypatch.setattr(main.ledger, "record_meeting", lambda *a, **k: None)


def _stub_finalize_offline(monkeypatch):
    """Finalize's outside deps stubbed, but leave_call left to the test."""
    monkeypatch.setattr(main.anam_client, "end_conversation", lambda c: None)
    monkeypatch.setattr(
        lifecycle, "post_meeting",
        lambda avatar, transcript, **kw: {"summary": "s", "actions": [], "checklist": []},
    )
    monkeypatch.setattr(main.ledger, "record_meeting", lambda *a, **k: None)
    monkeypatch.setattr(main.cedric, "deliver_ended", lambda *a, **k: True)


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://recall.test/leave")
    return httpx.HTTPStatusError(
        f"status {status_code}", request=req,
        response=httpx.Response(status_code, request=req),
    )


def _raise(exc):
    def _f(*a, **k):
        raise exc
    return _f


# ── the allowance: first signup + remaining math ─────────────────────────


def test_first_signup_org_has_900_seconds(cp):
    org = _org(cp, "fresh-signup")
    assert entitlements.remaining_seconds(org) == 900
    assert entitlements.usage_summary(org) == {
        "plan": "free",
        "included_seconds": 900,
        "used_seconds": 0,
        "remaining_seconds": 900,
    }


def test_remaining_seconds_math_closed_plus_active_elapsed(cp):
    org = _org(cp, "math")
    gate = entitlements.open_usage(org, "m-bot1", "laura")
    assert gate["ok"] is True and gate["remaining_seconds"] == 900
    # pending consumes NOTHING
    assert entitlements.remaining_seconds(org) == 900
    # active consumes live wall-clock from Recall's in_call timestamp
    deadline = entitlements.mark_in_call(org, "m-bot1", time.time() - 120)
    assert deadline is not None
    r = entitlements.remaining_seconds(org)
    assert 750 <= r <= 782, f"active elapsed not counted: {r}"  # ~900-120, loose
    # band: the elapsed keeps growing while the test itself runs
    # the deadline is in_call + remaining-at-that-moment (own elapsed excluded)
    row = entitlements.usage_row(org, "m-bot1")
    assert abs(row["deadline"] - (row["in_call_at"] + 900)) < 3
    # closed consumption is durable
    assert entitlements.close_usage(org, "m-bot1", 300, "ended") is True
    assert entitlements.remaining_seconds(org) == 600
    gate2 = entitlements.open_usage(org, "m-bot2", "cedric")
    assert gate2["ok"] is True and gate2["remaining_seconds"] == 600


def test_mark_in_call_is_idempotent(cp):
    org = _org(cp, "idem")
    entitlements.open_usage(org, "i-bot", "laura")
    t0 = time.time() - 60
    first = entitlements.mark_in_call(org, "i-bot", t0)
    again = entitlements.mark_in_call(org, "i-bot", time.time())  # later ts ignored
    # epsilon, not ==: the deadline round-trips through Postgres timestamptz
    # (microsecond truncation) so an exact float compare is ~50% flaky.
    assert abs(again - first) < 1e-3
    assert abs(entitlements.usage_row(org, "i-bot")["in_call_at"] - t0) < 2


# ── 10 min Laura + 5 min Cedric exhaust ONE shared allowance ─────────────


def test_shared_allowance_exhausted_across_avatars(cp):
    org = _org(cp, "shared")
    now = time.time()
    g1 = entitlements.open_usage(org, "s-laura", "laura")
    assert g1["ok"] is True
    entitlements.mark_in_call(org, "s-laura", now - 600)
    entitlements.close_usage(org, "s-laura", 600, "ended")     # 10 min of Laura
    g2 = entitlements.open_usage(org, "s-cedric", "cedric")
    assert g2["ok"] is True and g2["remaining_seconds"] == 300
    entitlements.mark_in_call(org, "s-cedric", now - 300)
    entitlements.close_usage(org, "s-cedric", 300, "ended")    # 5 min of Cedric
    assert entitlements.remaining_seconds(org) == 0
    refused = entitlements.open_usage(org, "s-third", "laura")
    assert refused == {"ok": False, "reason": "usage_limit_reached"}


def test_route_returns_exact_402_body_when_exhausted(cp, start_env):
    # Exhaust the Demo org (the anonymous /sessions/start tenant), then the
    # route must answer the EXACT 402 contract without touching Recall.
    demo = settings.demo_org_id
    now = time.time()
    entitlements.open_usage(demo, "d-laura", "laura")
    entitlements.mark_in_call(demo, "d-laura", now - 600)
    entitlements.close_usage(demo, "d-laura", 600, "ended")
    entitlements.open_usage(demo, "d-cedric", "cedric")
    entitlements.mark_in_call(demo, "d-cedric", now - 300)
    entitlements.close_usage(demo, "d-cedric", 300, "ended")
    assert entitlements.remaining_seconds(demo) == 0

    resp = TestClient(main.app).post("/sessions/start", json={"meeting_url": _MEET_URL})

    assert resp.status_code == 402
    assert resp.json() == {
        "error": "usage_limit_reached",
        "remaining_seconds": 0,
        "checkout_path": "/billing/checkout",
    }
    assert start_env == []  # create_bot NEVER called past the gate


def test_route_409_when_org_already_has_active_meeting(cp, start_env):
    # A second concurrent meeting for the SAME org is refused by the partial
    # unique index — surfaced as the exact 409 body. (Different meeting_url,
    # so the per-URL clash guard doesn't shadow the entitlement one.)
    entitlements.open_usage(settings.demo_org_id, "d-live", "laura")
    resp = TestClient(main.app).post(
        "/sessions/start", json={"meeting_url": "https://zoom.us/j/999888777"}
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": "active_session_exists"}
    assert start_env == []


def test_route_start_swaps_provisional_for_real_bot_id(cp, start_env):
    resp = TestClient(main.app).post("/sessions/start", json={"meeting_url": _MEET_URL})
    assert resp.status_code == 200
    bid = resp.json()["bot_id"]
    row = entitlements.usage_row(settings.demo_org_id, bid)
    assert row is not None and row["state"] == "pending"
    # no stranded provisional row: the org's single slot is held by the REAL id
    open_ids = {r["bot_id"] for r in entitlements.open_usage_rows()}
    assert open_ids == {bid}
    store.remove(bid)


# ── failed join consumes zero ────────────────────────────────────────────


def test_failed_join_consumes_zero(cp):
    org = _org(cp, "failedjoin")
    entitlements.open_usage(org, "f-bot", "laura")
    # never went in-call: whatever value is passed, a pending row closes at 0
    assert entitlements.close_usage(org, "f-bot", 999, "dispatch_failed") is True
    row = entitlements.usage_row(org, "f-bot")
    assert row["state"] == "closed" and row["consumed_seconds"] == 0
    assert entitlements.remaining_seconds(org) == 900
    # idempotent: the second close is a no-op and can't rewrite the value
    assert entitlements.close_usage(org, "f-bot", 500, "ended") is False
    assert entitlements.usage_row(org, "f-bot")["consumed_seconds"] == 0


def test_dispatch_failure_releases_pending_row(cp, start_env, monkeypatch):
    # create_bot blows up AFTER the gate passed: the pending row must close
    # (consumed 0, 'dispatch_failed') so the org can immediately retry.
    def boom(*a, **k):
        raise RuntimeError("recall down")

    monkeypatch.setattr(main.recall_client, "create_bot", boom)
    resp = TestClient(main.app).post("/sessions/start", json={"meeting_url": _MEET_URL})
    assert resp.status_code == 400
    assert entitlements.open_usage_rows() == []           # slot released
    assert entitlements.remaining_seconds(settings.demo_org_id) == 900


# ── silent meeting consumes wall-clock (status-based, no transcript) ─────


def test_silent_meeting_consumes_wall_clock(cp):
    org = _org(cp, "silent")
    entitlements.open_usage(org, "q-bot", "laura")
    now = time.time()
    # the clock is Recall-status-based: mark from the in-call status ts …
    entitlements.mark_in_call(org, "q-bot", now - 240)
    # … and close with the status-derived duration. No transcript anywhere.
    assert entitlements.close_usage(org, "q-bot", 240, "ended") is True
    assert entitlements.remaining_seconds(org) == 660
    assert entitlements.usage_row(org, "q-bot")["consumed_seconds"] == 240


# ── concurrency: racing starts cannot overspend ──────────────────────────


def test_concurrent_starts_exactly_one_wins(cp):
    org = _org(cp, "race-full")
    barrier = threading.Barrier(2)

    def gate(i):
        barrier.wait()
        return entitlements.open_usage(org, f"r-bot{i}", "laura")

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(gate, range(2)))

    oks = [r for r in results if r["ok"]]
    losers = [r for r in results if not r["ok"]]
    assert len(oks) == 1, f"the DB index must let exactly ONE through: {results}"
    assert losers[0]["reason"] == "active_session_exists"


def test_concurrent_starts_with_one_second_left(cp):
    # remaining=1s and two racing gates: the FOR UPDATE + partial unique index
    # still admit exactly one — never two paid bots on a 1-second balance.
    org = _org(cp, "race-1s")
    entitlements.open_usage(org, "r1-old", "laura")
    entitlements.mark_in_call(org, "r1-old", time.time() - 899)
    entitlements.close_usage(org, "r1-old", 899, "ended")
    assert entitlements.remaining_seconds(org) == 1

    barrier = threading.Barrier(2)

    def gate(i):
        barrier.wait()
        return entitlements.open_usage(org, f"r1-bot{i}", "laura")

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(gate, range(2)))

    assert sum(1 for r in results if r["ok"]) == 1, f"overspend: {results}"


# ── the limit stops the avatar and preserves the artifact ────────────────


def test_limit_stops_avatar_and_preserves_artifact(cp, fresh_store, monkeypatch):
    _stub_finalize_vendors(monkeypatch)
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    org = _org(cp, "limit-live")
    session = store.create("bot_lim", _MEET_URL, "laura", org_id=org)
    session.memory_brief = ""
    session.add_utterance("Ben", "Let's keep going.")
    # deadline already in the past: in-call 1000s ago on a 900s allowance
    entitlements.open_usage(org, "bot_lim", "laura")
    entitlements.mark_in_call(org, "bot_lim", time.time() - 1000)
    left: list[str] = []
    monkeypatch.setattr(main.recall_client, "leave_call", left.append)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            _bot_json("bot_lim", [("in_call_recording", time.time() - 1000)])
        ),
    )

    asyncio.run(main._reconcile_once())

    assert left == ["bot_lim"]                        # the meter was STOPPED
    assert store.get("bot_lim") is None               # session finalized
    artifact = store.get_artifact("bot_lim")
    assert artifact is not None                       # deliverable PRESERVED
    assert artifact["summary"] == "s"
    row = entitlements.usage_row(org, "bot_lim")
    assert row["state"] == "closed"
    assert row["close_reason"] == "limit_reached"
    assert 890 <= row["consumed_seconds"] <= 905      # capped at the deadline


def test_usage_warning_speaks_once_near_deadline(cp, fresh_store, monkeypatch):
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    org = _org(cp, "warn")
    session = store.create("bot_warn", _MEET_URL, "laura", org_id=org)
    session.memory_brief = ""
    entitlements.open_usage(org, "bot_warn", "laura")
    # ~3 minutes left → inside the 5-min window, outside the 1-min one
    entitlements.mark_in_call(org, "bot_warn", time.time() - 720)
    spoken: list[str] = []

    async def fake_speak(sess, text, *a, **k):
        spoken.append(text)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            _bot_json("bot_warn", [("in_call_recording", time.time() - 720)])
        ),
    )

    asyncio.run(main._reconcile_once())
    asyncio.run(main._reconcile_once())  # second pass must NOT repeat it

    assert len(spoken) == 1                            # one-shot heads-up
    assert store.get("bot_warn") is not None           # still live, not cut off
    store.remove("bot_warn")
    entitlements.close_usage(org, "bot_warn", 0, "ended")   # tidy the module PG


# ── restart restore: the local store is gone, enforcement is not ─────────


def test_restart_restores_deadline_and_stops_orphan(cp, fresh_store, monkeypatch):
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    org = _org(cp, "restore-live")
    entitlements.open_usage(org, "bot_orphan", "laura")
    entitlements.mark_in_call(org, "bot_orphan", time.time() - 2000)  # way past deadline
    assert store.all_sessions() == []                  # the store was WIPED
    left: list[str] = []
    monkeypatch.setattr(main.recall_client, "leave_call", left.append)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            _bot_json("bot_orphan", [("in_call_recording", time.time() - 2000)])
        ),
    )

    asyncio.run(main._reconcile_once())

    assert left == ["bot_orphan"]                      # enforced from PG alone
    row = entitlements.usage_row(org, "bot_orphan")
    assert row["state"] == "closed"
    assert row["close_reason"] == "limit_reached"


def test_restart_closes_terminal_orphan_from_recall_timestamps(
    cp, fresh_store, monkeypatch
):
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    org = _org(cp, "restore-done")
    now = time.time()
    entitlements.open_usage(org, "bot_done", "laura")
    entitlements.mark_in_call(org, "bot_done", now - 500)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            _bot_json(
                "bot_done",
                [("in_call_recording", now - 500), ("done", now - 200)],
            )
        ),
    )

    asyncio.run(main._reconcile_once())

    row = entitlements.usage_row(org, "bot_done")
    assert row["state"] == "closed" and row["close_reason"] == "ended"
    assert 295 <= row["consumed_seconds"] <= 305       # done_ts - in_call_ts


def test_restart_starts_clock_for_orphan_from_recall_status(
    cp, fresh_store, monkeypatch
):
    # A pending row + a live in-call bot after a restart: the pass must START
    # the clock from Recall's own status timestamp (not just enforce old ones).
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    org = _org(cp, "restore-clock")
    now = time.time()
    entitlements.open_usage(org, "bot_clock", "laura")
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            _bot_json("bot_clock", [("in_call_recording", now - 50)])
        ),
    )

    asyncio.run(main._reconcile_once())

    row = entitlements.usage_row(org, "bot_clock")
    assert row["state"] == "active"
    assert abs(row["in_call_at"] - (now - 50)) < 3     # Recall's ts, not ours
    assert abs(row["deadline"] - (now - 50 + 900)) < 5
    entitlements.close_usage(org, "bot_clock", 0, "ended")  # tidy the module PG


def test_orphaned_provisional_row_is_released(cp, fresh_store, monkeypatch, pg):
    # A 'pending:<uuid>' row older than the grace window (the process died
    # between the gate and the bot-id swap) frees the org's slot, consuming 0.
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    org = _org(cp, "provisional")
    prov = f"pending:{uuid.uuid4().hex}"
    entitlements.open_usage(org, prov, "laura")
    with _admin(pg) as conn:  # age the row past the 600s grace
        conn.execute(
            "UPDATE usage_sessions SET created_at = now() - interval '20 minutes' "
            "WHERE bot_id = %s",
            (prov,),
        )

    def no_poll(*a, **k):
        raise AssertionError("a pending: row must never be polled against Recall")

    monkeypatch.setattr(main.httpx, "get", no_poll)

    asyncio.run(main._reconcile_once())

    row = entitlements.usage_row(org, prov)
    assert row["state"] == "closed" and row["consumed_seconds"] == 0
    assert row["close_reason"] == "orphaned"
    assert entitlements.remaining_seconds(org) == 900
    gate = entitlements.open_usage(org, "next-meeting", "laura")
    assert gate["ok"] is True                          # the slot is free again


# ── billing DB outage: fail CLOSED before any vendor call ────────────────


def test_billing_outage_503_before_create_bot(start_env, monkeypatch):
    # Control plane "configured" but the DB is down: open_usage raises →
    # 503 billing_unavailable and create_bot is NEVER reached. (Key-free
    # harness: no real engine anywhere — enabled() is stubbed.)
    monkeypatch.setattr(main.control_plane, "enabled", lambda: True)

    def outage(*a, **k):
        raise entitlements.EntitlementsUnavailable("OperationalError")

    monkeypatch.setattr(main.entitlements, "open_usage", outage)

    resp = TestClient(main.app).post("/sessions/start", json={"meeting_url": _MEET_URL})

    assert resp.status_code == 503
    assert resp.json() == {"error": "billing_unavailable"}
    assert start_env == []                             # no vendor dispatch


# ── key-free demo: byte-identical when the control plane is disabled ─────


def test_key_free_start_unchanged_no_gate_no_engine(start_env, monkeypatch):
    # LAURA_DATABASE_URL empty (the suite default): the gate must not run at
    # all — no 402/409/503 path, no engine, exactly today's happy path.
    assert control_plane.enabled() is False
    calls: list = []

    def spy(*a, **k):
        calls.append(a)
        raise AssertionError("open_usage must not be called in the key-free demo")

    monkeypatch.setattr(main.entitlements, "open_usage", spy)

    resp = TestClient(main.app).post("/sessions/start", json={"meeting_url": _MEET_URL})

    assert resp.status_code == 200
    assert "bot_id" in resp.json()
    assert calls == []                                 # gate never consulted
    assert control_plane._engine is None               # no engine was created
    store.remove(resp.json()["bot_id"])


def test_key_free_entitlement_api_is_inert():
    # Every DAL function no-ops when disabled — the offline contract.
    assert entitlements.remaining_seconds("any-org") is None
    assert entitlements.open_usage("any-org", "any-bot") is None
    assert entitlements.mark_in_call("any-org", "any-bot", time.time()) is None
    assert entitlements.close_usage("any-org", "any-bot", 10, "ended") is False
    assert entitlements.open_usage_rows() == []
    assert entitlements.usage_row("any-org", "any-bot") is None
    assert entitlements.usage_summary("any-org") is None
    assert control_plane._engine is None


# ── RLS on the NEW 0003 table, as the policy-bound app role ─────────────


def test_rls_isolates_usage_sessions_for_app_role(cp, pg):
    a, b = _org(cp, "rls-a"), _org(cp, "rls-b")
    entitlements.open_usage(a, "rls-bot-a", "laura")
    entitlements.open_usage(b, "rls-bot-b", "laura")
    import psycopg.conninfo as _ci

    app_kwargs = {
        **_ci.conninfo_to_dict(pg["uri"]),
        "user": APP_ROLE,
        "password": "pw",
    }
    with psycopg.connect(**app_kwargs, autocommit=True) as conn:
        is_super = conn.execute(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()[0]
        assert not is_super, "app role must be policy-bound for this to mean anything"
        # no org context → FORCE RLS yields nothing at all
        assert conn.execute("SELECT count(*) FROM usage_sessions").fetchone()[0] == 0
        # as org A: a bare, WHERE-less SELECT sees ONLY org A's row
        conn.execute("SELECT set_config('app.current_org', %s, false)", (a,))
        got = {
            str(r[0])
            for r in conn.execute("SELECT org_id FROM usage_sessions").fetchall()
        }
        assert got == {a}


# ── BLOCKER 1: provisional-bot_id swap failure must NOT run free-forever ──


def test_swap_failure_fails_closed_and_stops_born_bot(
    cp, start_env, fresh_store, monkeypatch
):
    # A false/no-row provisional swap is NOT success. A born bot without a
    # durable meter is stopped before the route can return it to the customer.
    _stub_finalize_vendors(monkeypatch)
    left: list[str] = []
    monkeypatch.setattr(main.recall_client, "leave_call", left.append)
    monkeypatch.setattr(
        main.entitlements, "assign_bot_id", lambda org, provisional, real: False
    )

    resp = TestClient(main.app).post(
        "/sessions/start", json={"meeting_url": _MEET_URL}
    )

    assert resp.status_code == 503
    assert resp.json() == {"error": "billing_unavailable"}
    assert left == ["bot_1"]
    assert store.get("bot_1") is None
    assert entitlements.open_usage_rows() == []


def test_swap_failure_and_unverified_leave_repairs_meter_before_retry(
    cp, start_env, fresh_store, monkeypatch
):
    # Exact P0: binding fails, then the immediate Recall leave also fails.
    # The kept leave_pending session must rebind + start its clock BEFORE the
    # next leave retry, so a still-running vendor bot is never unmetered.
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    _stub_finalize_offline(monkeypatch)
    real_assign = entitlements.assign_bot_id
    db_recovered = {"value": False}

    def assign(org_id, provisional, real):
        if not db_recovered["value"]:
            return False
        return real_assign(org_id, provisional, real)

    monkeypatch.setattr(main.entitlements, "assign_bot_id", assign)
    leave_recovers = {"value": False}
    leave_attempts: list[str] = []

    def leave(bot_id):
        leave_attempts.append(bot_id)
        if not leave_recovers["value"]:
            raise _http_status_error(503)

    monkeypatch.setattr(main.recall_client, "leave_call", leave)

    resp = TestClient(main.app).post(
        "/sessions/start", json={"meeting_url": _MEET_URL}
    )

    assert resp.status_code == 503
    assert resp.json() == {"error": "billing_unavailable"}
    kept = store.get("bot_1")
    assert kept is not None and kept.leave_pending is True
    stranded = entitlements.open_usage_rows()
    assert len(stranded) == 1
    provisional = stranded[0]["bot_id"]
    assert provisional.startswith("pending:")

    # DB recovers; Recall confirms the bot is in-call, but leave is still 503.
    db_recovered["value"] = True
    started = time.time() - 30
    monkeypatch.setattr(
        main.httpx,
        "get",
        lambda url, *, headers, timeout: _FakeResponse(
            _bot_json("bot_1", [("in_call_recording", started)])
        ),
    )
    asyncio.run(main._reconcile_once())

    assert store.get("bot_1") is not None
    assert entitlements.usage_row(settings.demo_org_id, provisional) is None
    metered = entitlements.usage_row(settings.demo_org_id, "bot_1")
    assert metered is not None and metered["state"] == "active"
    assert abs(metered["in_call_at"] - started) < 3
    assert metered["deadline"] > time.time()
    assert {row["bot_id"] for row in entitlements.open_usage_rows()} == {"bot_1"}

    # Once Recall verifies the stop, the already-delivered session is removed
    # and the now-real usage row closes exactly once.
    leave_recovers["value"] = True
    asyncio.run(main._reconcile_once())
    assert store.get("bot_1") is None
    closed = entitlements.usage_row(settings.demo_org_id, "bot_1")
    assert closed["state"] == "closed"
    assert closed["close_reason"] == "usage_binding_failed"
    assert len(leave_attempts) == 3

def test_untracked_live_bot_over_budget_is_cut_off(cp, fresh_store, monkeypatch):
    # A live local session whose org is EXHAUSTED and has no usage row (swap lost
    # + orphan already freed the provisional) can't be metered → it must be
    # force-finalized (meter stopped, artifact preserved), never run free.
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    _stub_finalize_vendors(monkeypatch)
    org = _org(cp, "untracked-broke")
    # exhaust the org with a prior closed meeting
    entitlements.open_usage(org, "prior", "laura")
    entitlements.mark_in_call(org, "prior", time.time() - 900)
    entitlements.close_usage(org, "prior", 900, "ended")
    assert entitlements.remaining_seconds(org) == 0

    s = store.create("ghost-bot", _MEET_URL, "laura", org_id=org)
    s.memory_brief = ""
    s.add_utterance("Ben", "still talking")           # untracked, no usage row
    left: list[str] = []
    monkeypatch.setattr(main.recall_client, "leave_call", left.append)
    monkeypatch.setattr(
        main.httpx, "get",
        lambda url, *, headers, timeout: _FakeResponse(
            _bot_json("ghost-bot", [("in_call_recording", time.time() - 60)])
        ),
    )

    asyncio.run(main._reconcile_once())

    assert left == ["ghost-bot"]                       # cut off, not free-forever
    assert store.get("ghost-bot") is None              # finalized
    assert store.get_artifact("ghost-bot") is not None  # artifact preserved


def test_orphan_sweep_preserves_slot_while_org_is_live(cp, fresh_store):
    # A stranded provisional row must NOT be closed (freeing the slot) while the
    # owning org still has a live local session — only a truly orphaned org frees.
    org = _org(cp, "orphan-live")
    prov = f"pending:{uuid.uuid4().hex}"
    entitlements.open_usage(org, prov, "laura")
    usage = {
        "org_id": org, "bot_id": prov, "avatar_id": "laura", "state": "pending",
        "created_at": time.time() - 1200,  # older than the 600s grace
        "in_call_at": None, "deadline": None,
    }
    # org has a LIVE session → slot preserved
    asyncio.run(main._reconcile_usage_orphan(prov, usage, {org}))
    assert entitlements.usage_row(org, prov)["state"] == "pending"
    # org has no live session → now a true orphan, freed
    asyncio.run(main._reconcile_usage_orphan(prov, usage, set()))
    closed = entitlements.usage_row(org, prov)
    assert closed["state"] == "closed" and closed["close_reason"] == "orphaned"


# ── BLOCKER 2: don't close/free the slot before the leave is verified stopped ─


def test_finalize_unverified_leave_holds_slot_until_retry(cp, fresh_store, monkeypatch):
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    _stub_finalize_offline(monkeypatch)
    org = _org(cp, "b2-finalize")
    s = store.create("b2-bot", _MEET_URL, "laura", org_id=org)
    s.memory_brief = ""
    s.add_utterance("Ben", "ship the DPA to Acme")
    entitlements.open_usage(org, "b2-bot", "laura")
    entitlements.mark_in_call(org, "b2-bot", time.time() - 120)
    monkeypatch.setattr(
        main.recall_client, "leave_call", _raise(_http_status_error(503))
    )

    asyncio.run(main._finalize_session("b2-bot", source="webhook"))

    # usage NOT closed, slot NOT freed, session kept for the reconcile retry
    row = entitlements.usage_row(org, "b2-bot")
    assert row["state"] == "active"
    kept = store.get("b2-bot")
    assert kept is not None and kept.leave_pending is True
    # a 2nd meeting for the org is correctly REFUSED while the meter is unverified
    assert (
        entitlements.open_usage(org, "b2-second", "laura")["reason"]
        == "active_session_exists"
    )

    # the leave now confirms → _retry_leave closes the row ONCE, honest consumed
    monkeypatch.setattr(main.recall_client, "leave_call", lambda b: None)
    assert asyncio.run(main._retry_leave("b2-bot", kept)) is True
    closed = entitlements.usage_row(org, "b2-bot")
    assert closed["state"] == "closed"
    assert closed["close_reason"] == "ended"
    assert 115 <= closed["consumed_seconds"] <= 140    # ~ now - in_call (120s)
    assert store.get("b2-bot") is None
    # slot freed only now
    assert entitlements.open_usage(org, "b2-after", "laura")["ok"] is True


def test_cancel_unverified_leave_holds_slot(cp, fresh_store, monkeypatch):
    # cancel_session used to close+remove regardless of the leave outcome; on an
    # unverified leave the slot must stay held (the bot may still bill).
    monkeypatch.setattr(settings, "recall_api_key", "recall-key")
    monkeypatch.setattr(main.anam_client, "end_conversation", lambda c: None)
    org = _org(cp, "cancel-b2")
    store.create("c-bot", _MEET_URL, "laura", org_id=org)
    entitlements.open_usage(org, "c-bot", "laura")
    entitlements.mark_in_call(org, "c-bot", time.time() - 60)
    monkeypatch.setattr(
        main.recall_client, "leave_call", _raise(_http_status_error(503))
    )
    monkeypatch.setattr(
        main.recall_client, "delete_bot", _raise(_http_status_error(503))
    )

    resp = TestClient(main.app).post("/sessions/c-bot/cancel")

    assert resp.status_code == 202 and resp.json()["cancelled"] is False
    row = entitlements.usage_row(org, "c-bot")
    assert row["state"] == "active"                    # slot held, not freed
    assert store.get("c-bot").leave_pending is True
    assert (
        entitlements.open_usage(org, "c-other", "laura")["reason"]
        == "active_session_exists"
    )
    store.remove("c-bot")
    entitlements.close_usage(org, "c-bot", 0, "ended")      # tidy the module PG

# ── app-role enforcement + per-bot tenant scoping ────────────────────────


def test_entitlement_runtime_is_exact_policy_bound_role(cp):
    from sqlalchemy import text

    with cp._get_engine().connect() as conn:
        assert conn.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).fetchone() == (APP_ROLE, False, False)


def test_per_bot_operations_cannot_cross_org(cp):
    a, b = _org(cp, "per-bot-a"), _org(cp, "per-bot-b")
    assert entitlements.open_usage(a, "tenant-bot", "laura")["ok"] is True

    # Knowing a foreign bot id is never enough: every per-bot operation pins
    # RLS before its first query and includes org_id in the statement itself.
    assert entitlements.usage_row(b, "tenant-bot") is None
    assert entitlements.mark_in_call(b, "tenant-bot", time.time()) is None
    assert entitlements.close_usage(b, "tenant-bot", 900, "ended") is False
    assert entitlements.assign_bot_id(b, "tenant-bot", "stolen-bot") is False

    own = entitlements.usage_row(a, "tenant-bot")
    assert own is not None and own["state"] == "pending"
    assert entitlements.assign_bot_id(a, "tenant-bot", "owned-bot") is True
    assert entitlements.usage_row(a, "owned-bot") is not None
    assert entitlements.usage_row(b, "owned-bot") is None


def test_restart_enumeration_uses_private_read_only_definer(cp, pg):
    a, b = _org(cp, "restart-a"), _org(cp, "restart-b")
    entitlements.open_usage(a, "restart-bot-a", "laura")
    entitlements.open_usage(b, "restart-bot-b", "cedric")

    # The one intentionally global operation is an exact private definer.
    assert {row["org_id"] for row in entitlements.open_usage_rows()} == {a, b}

    import psycopg.conninfo as _ci

    kwargs = {
        **_ci.conninfo_to_dict(pg["uri"]),
        "user": APP_ROLE,
        "password": "pw",
    }
    with psycopg.connect(**kwargs, autocommit=True) as conn:
        assert conn.execute(
            "SELECT current_user, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone() == (APP_ROLE, False, False)
        # A bare runtime SELECT remains RLS-bound and cannot enumerate tenants.
        assert conn.execute("SELECT count(*) FROM usage_sessions").fetchone()[0] == 0

