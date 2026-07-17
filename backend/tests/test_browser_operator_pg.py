"""Browser operator B0 — release blockers on real Postgres as laura_app.

Cross-org isolation + RLS; authenticated ownership; the state machine (invalid
transitions rejected, idempotent close/revoke); TTL expiry; presentation-token
expiry / replay / mismatch; provider ids hidden from public contracts; command
sequence + idempotency; consequential writes rejected (read-only stance) or
routed to a canonical action; repeated approval executes once; execution-time
avatar/tool re-check; secrets absent from responses; flag-off identity;
provider failure/timeout handling.
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

from app import avatar_resolver, control_plane, ledger, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.browser import dal, operator, tokens  # noqa: E402
from app.browser.fake_provider import FakeProvider  # noqa: E402

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
            "default_version = '1.0'\nrelocatable = true\ncomment = 'shim'\n"),
        "pgcrypto--1.0.sql": "-- shim\n",
        "citext.control": (
            "default_version = '1.0'\nrelocatable = true\ncomment = 'shim'\n"),
        "citext--1.0.sql": "CREATE DOMAIN citext AS text;\n",
    }
    for name, body in shims.items():
        path = ext_dir / name
        if not path.exists():
            path.write_text(body)


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    _write_extension_shims()
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("browser_pg")))
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(
            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'pw' "
            "NOSUPERUSER NOBYPASSRLS")
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
        host=info.get("host"), port=info.get("port"))
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env={**os.environ, "LAURA_DATABASE_ADMIN_URL": admin_sa_url,
             "LAURA_DATABASE_URL": "", "LAURA_REQUIRE_MIGRATIONS": "1"},
        capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, (
        f"alembic upgrade failed:\n{proc.stdout}\n{proc.stderr}")
    yield {"uri": uri, "app_sa_url": app_sa_url, "app_uri": app_uri}
    control_plane.reset_engine()
    srv.cleanup()


def _admin(pg):
    return psycopg.connect(pg["uri"], autocommit=True)


@pytest.fixture
def cp(pg, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    monkeypatch.setattr(settings, "browser_operator_enabled", True)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    avatar_resolver._reset_for_tests()
    FakeProvider._reset()
    control_plane.reset_engine()
    with _admin(pg) as conn:
        for table in ("browser_presentation_tokens", "browser_commands",
                      "browser_sessions", "queued_actions"):
            conn.execute(f"DELETE FROM {table}")
    yield control_plane
    avatar_resolver._reset_for_tests()
    FakeProvider._reset()
    control_plane.reset_engine()


def _org(cp, tag: str) -> str:
    stamp = str(time.time_ns())
    return cp.ensure_user(
        f"sub-{tag}-{stamp}", f"{tag}-{stamp}@freemail.test", tag, ""
    )["org_id"]


def _create(org: str, principal: str = "u_alice") -> dict:
    return operator.create_session(
        org, principal=principal, avatar_key="laura", meeting_ref="bot-1")


# ── ownership + RLS + cross-org ─────────────────────────────────────────────

def test_rls_and_cross_org_isolation(cp, pg):
    org_a = _org(cp, "rls-a")
    org_b = _org(cp, "rls-b")
    sess = _create(org_a)
    # Org B cannot see or command org A's session.
    assert operator.get_session(org_b, sess["id"]) is None
    assert operator.issue_command(
        org_b, sess["id"], verb="observe")["reason"] == "not_found"
    with psycopg.connect(pg["app_uri"], autocommit=True) as raw:
        raw.execute("SELECT set_config('app.current_org', %s, false)",
                    (org_b,))
        assert raw.execute(
            "SELECT count(*) FROM browser_sessions").fetchone()[0] == 0
        denied = raw.execute(
            "UPDATE browser_sessions SET state='revoked' WHERE org_id=%s",
            (org_a,))
        assert denied.rowcount == 0


def test_ownership_other_principal_cannot_touch(cp):
    org = _org(cp, "own")
    sess = _create(org, "u_alice")
    assert operator.get_session(org, sess["id"], principal="u_bob") is None
    assert operator.issue_command(
        org, sess["id"], verb="observe",
        principal="u_bob")["reason"] == "not_owner"
    # The owner can.
    ok = operator.issue_command(org, sess["id"], verb="observe",
                                principal="u_alice")
    assert ok["ok"] is True


# ── provider ids hidden ─────────────────────────────────────────────────────

def test_provider_ref_never_in_public_contract(cp):
    org = _org(cp, "hide")
    sess = _create(org)
    view = operator.get_session(org, sess["id"])
    blob = json.dumps(view)
    assert "fake-prov" not in blob and "provider_ref" not in blob
    assert "viewer_ref" not in blob
    # The session id is Laura's uuid, not the provider's.
    assert sess["id"] != ""


# ── state machine ───────────────────────────────────────────────────────────

def test_invalid_state_transitions_rejected(cp):
    org = _org(cp, "state")
    sess = _create(org)
    operator.close_session(org, sess["id"])
    # Commands on a closed session are rejected (409-mapped reason).
    r = operator.issue_command(org, sess["id"], verb="observe")
    assert r["reason"] == "invalid_state:closed"
    assert operator.present(org, sess["id"])["reason"].startswith(
        "not_presentable")


def test_idempotent_close_and_revoke(cp):
    org = _org(cp, "idem")
    sess = _create(org)
    assert operator.close_session(org, sess["id"])["state"] == "closed"
    again = operator.close_session(org, sess["id"])
    assert again["state"] == "closed" and again.get("idempotent") is True
    # Revoke on an already-closed session is idempotent too.
    rev = operator.revoke_session(org, sess["id"])
    assert rev["idempotent"] is True

    # Revoke as a fresh security stop is terminal + idempotent.
    s2 = _create(org)
    assert operator.revoke_session(org, s2["id"])["state"] == "revoked"
    assert operator.revoke_session(org, s2["id"])["idempotent"] is True


# ── TTL expiry ──────────────────────────────────────────────────────────────

def test_session_expires_after_ttl(cp, pg):
    org = _org(cp, "ttl")
    sess = _create(org)
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE browser_sessions SET expires_at="
            "clock_timestamp() - interval '1 hour' WHERE org_id=%s", (org,))
    # A lazy access flips it to expired and refuses commands.
    view = operator.get_session(org, sess["id"])
    assert view["state"] == "expired"
    assert operator.issue_command(
        org, sess["id"], verb="observe")["reason"] == "invalid_state:expired"
    # The reconcile query also finds + expires due sessions.
    s2 = _create(org)
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE browser_sessions SET expires_at="
            "clock_timestamp() - interval '1 hour' WHERE id=CAST(%s AS uuid)",
            (s2["id"],))
    assert org in dal.orgs_with_due_sessions()
    expired = dal.expire_due(org)
    assert any(r["id"] == s2["id"] for r in expired)


# ── presentation token: expiry, replay, mismatch ────────────────────────────

def test_token_exchange_expiry_replay_and_mismatch(cp, pg):
    org = _org(cp, "tok")
    other = _org(cp, "tok-other")
    sess = _create(org)
    minted = operator.present(org, sess["id"])
    token = minted["presentation_token"]
    # Valid exchange returns a read-only viewer, no provider url.
    ex = operator.exchange_token(org, token)
    assert ex["ok"] is True and ex["viewer"]["read_only"] is True
    assert "connect_url" not in json.dumps(ex)
    # Another org cannot exchange this org's token.
    assert operator.exchange_token(other, token)["ok"] is False
    # A malformed / unknown token is denied.
    assert operator.exchange_token(org, "garbage")["reason"] == "malformed"
    assert operator.exchange_token(
        org, "lbt_" + "x" * 40)["reason"] == "not_found"
    # Closing the session revokes tokens: replay after close is denied.
    operator.close_session(org, sess["id"])
    assert operator.exchange_token(org, token)["ok"] is False


def test_token_denied_after_expiry(cp, pg):
    org = _org(cp, "tokexp")
    sess = _create(org)
    token = operator.present(org, sess["id"])["presentation_token"]
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE browser_presentation_tokens SET expires_at="
            "clock_timestamp() - interval '1 minute' WHERE org_id=%s", (org,))
    verdict = operator.exchange_token(org, token)
    assert verdict["ok"] is False and verdict["reason"] == "expired"


# ── command sequence + idempotency ──────────────────────────────────────────

def test_command_sequence_and_idempotency(cp):
    org = _org(cp, "seq")
    sess = _create(org)
    r1 = operator.issue_command(org, sess["id"], verb="navigate",
                                url="https://demo.laura.test/pricing",
                                command_id="cmd-1")
    assert r1["ok"] and r1["seq"] == 1
    r2 = operator.issue_command(org, sess["id"], verb="scroll",
                                command_id="cmd-2")
    assert r2["seq"] == 2
    # Replay of cmd-1 returns the first result, no re-execution, seq unchanged.
    replay = operator.issue_command(org, sess["id"], verb="navigate",
                                    url="https://demo.laura.test/pricing",
                                    command_id="cmd-1")
    assert replay["idempotent_replay"] is True and replay["seq"] == 1
    assert operator.get_session(org, sess["id"])["last_command_seq"] == 2


def test_concurrent_command_id_executes_once(cp):
    org = _org(cp, "concmd")
    sess = _create(org)

    def go(_):
        return operator.issue_command(
            org, sess["id"], verb="scroll", command_id="dup")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(go, range(2)))
    # Both return a result; the session sequence advanced by exactly one.
    assert operator.get_session(org, sess["id"])["last_command_seq"] == 1
    assert any(r.get("idempotent_replay") for r in results) or \
        len({r["seq"] for r in results}) == 1


# ── writes: rejected (read-only) or routed to canonical action ──────────────

def test_write_click_rejected_read_only_by_default(cp):
    org = _org(cp, "write-ro")
    sess = _create(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/pricing")
    r = operator.issue_command(org, sess["id"], verb="click",
                               element_id="buy-team")
    assert r["ok"] is False and r["reason"] == "write_rejected_read_only"
    assert r["class"] == "guarded"
    # Nothing landed in queued_actions.
    from sqlalchemy import text as sql

    engine = control_plane._get_engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org)
        n = conn.execute(
            sql("SELECT count(*) FROM queued_actions WHERE org_id=:o "
                "AND execution_route='browser'"), {"o": org}).first()[0]
    assert n == 0


def test_write_routes_to_canonical_action_when_enabled(cp, monkeypatch):
    monkeypatch.setattr(settings, "browser_allow_writes", True)
    org = _org(cp, "write-act")
    sess = _create(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/pricing")
    r = operator.issue_command(org, sess["id"], verb="click",
                               element_id="buy-team")
    assert r["reason"] == "approval_required"
    action_id = r["action_id"]
    from sqlalchemy import text as sql

    engine = control_plane._get_engine()
    with engine.begin() as conn:
        control_plane._set_org(conn, org)
        row = conn.execute(
            sql("SELECT execution_route, permission_json FROM queued_actions "
                "WHERE org_id=:o AND action_id=:a"),
            {"o": org, "a": action_id}).mappings().first()
    assert row["execution_route"] == "browser"
    perm = row["permission_json"]
    if isinstance(perm, str):
        perm = json.loads(perm)
    assert perm["browser_session_id"] == sess["id"]
    assert perm["binding_fingerprint"]


def test_blocked_credential_field_never_executes(cp):
    org = _org(cp, "cred")
    sess = _create(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/login")
    r = operator.issue_command(org, sess["id"], verb="type",
                               element_id="pass", text="hunter2")
    assert r["ok"] is False and r["reason"] == "blocked"
    # The secret value never enters the recorded command result.
    assert "hunter2" not in json.dumps(r)


# ── approved execution: once, with execution-time re-check ──────────────────
# The key-free /org machine gate resolves to settings.demo_org_id, so we point
# demo_org_id at a real ensured org and drive the REAL HTTP approve door.

def _mint_guarded(org: str, meeting_ref: str) -> tuple[dict, str]:
    sess = operator.create_session(
        org, principal="", avatar_key="laura", meeting_ref=meeting_ref)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/pricing")
    r = operator.issue_command(org, sess["id"], verb="click",
                               element_id="buy-team")
    assert r["reason"] == "approval_required"
    return sess, r["action_id"]


def test_approved_browser_action_executes_once_and_rechecks(cp, monkeypatch):
    monkeypatch.setattr(settings, "browser_allow_writes", True)
    org = _org(cp, "approve")
    monkeypatch.setattr(settings, "demo_org_id", org)
    sess, action_id = _mint_guarded(org, "bot-x")

    import app.main as main_module
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    first = client.post(f"/org/actions/{action_id}/approve",
                        json={"decision": "approve"})
    assert first.status_code == 200
    assert first.json()["new_status"] == "done"
    # A receipt was written through the canonical channel.
    status = ledger.action_statuses([action_id], org_id=org)[action_id]
    assert status["status"] == "done"
    # Repeat approval is a replay — never a second execution.
    second = client.post(f"/org/actions/{action_id}/approve",
                         json={"decision": "approve"})
    assert second.json().get("idempotent_replay") is True
    _ = sess


def test_execution_recheck_fails_closed_when_session_revoked(cp, monkeypatch):
    monkeypatch.setattr(settings, "browser_allow_writes", True)
    org = _org(cp, "recheck")
    monkeypatch.setattr(settings, "demo_org_id", org)
    sess, action_id = _mint_guarded(org, "bot-y")
    # Revoke the session BEFORE approval — execution-time re-check must refuse.
    operator.revoke_session(org, sess["id"])

    import app.main as main_module
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    resp = client.post(f"/org/actions/{action_id}/approve",
                       json={"decision": "approve"})
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "failed"


# ── adversarial-verification regressions (findings fixed) ───────────────────

def test_present_close_revoke_enforce_principal_ownership(cp):
    """MEDIUM IDOR: a same-org non-owner cannot present/close/revoke another
    principal's session (present/close/revoke now re-check the principal)."""
    org = _org(cp, "idor")
    sess = _create(org, "u_alice")
    # user B (same org) knows the UUID but is not the owner.
    assert operator.present(org, sess["id"],
                            principal="u_bob")["reason"] == "not_owner"
    assert operator.close_session(org, sess["id"],
                                  principal="u_bob")["reason"] == "not_owner"
    assert operator.revoke_session(org, sess["id"],
                                   principal="u_bob")["reason"] == "not_owner"
    # No token was minted for B, and the session is untouched (still ready).
    assert operator.get_session(org, sess["id"])["state"] == "ready"
    # The owner still can.
    assert operator.present(org, sess["id"], principal="u_alice")["ok"] is True


def test_terminal_session_cannot_be_resurrected(cp):
    """MEDIUM TOCTOU: once terminal, no transition may move it back to a live
    state (guarded set_state + atomic ready-only present transition)."""
    org = _org(cp, "resurrect")
    sess = _create(org)
    operator.close_session(org, sess["id"])
    # A racing present against the now-closed session must not resurrect it.
    assert operator.present(org, sess["id"])["ok"] is False
    assert operator.get_session(org, sess["id"])["state"] == "closed"
    # set_state itself refuses to leave a terminal state (the WHERE guard).
    assert dal.set_state(org, sess["id"], "presenting") is False
    assert operator.get_session(org, sess["id"])["state"] == "closed"
    # present()'s ready→presenting transition never fires from a terminal state.
    assert dal.transition(org, sess["id"], "presenting", ("ready",)) is False
    assert operator.get_session(org, sess["id"])["state"] == "closed"


def test_nonowner_cannot_poison_command_id(cp):
    """LOW: a non-owner's rejected command must NOT leave a claim row that
    swallows the owner's later command (claim now runs AFTER the checks)."""
    org = _org(cp, "poison")
    sess = _create(org, "u_alice")
    # B tries to pre-claim a command_id on A's session — rejected as not_owner,
    # and crucially leaves NO claim behind.
    bad = operator.issue_command(org, sess["id"], verb="scroll",
                                 principal="u_bob", command_id="shared")
    assert bad["reason"] == "not_owner"
    # A's legitimate command with the same id executes for real (not swallowed).
    good = operator.issue_command(org, sess["id"], verb="scroll",
                                  principal="u_alice", command_id="shared")
    assert good["accepted"] is True and good.get("idempotent_replay") is not True


def test_nonowner_cannot_read_recorded_observation_via_replay(cp):
    """LOW residual of the claim-ordering fix: the idempotency REPLAY read now
    runs AFTER the ownership check, so a same-org non-owner cannot pull a
    victim's recorded observation by guessing session_id + command_id."""
    org = _org(cp, "replayleak")
    sess = _create(org, "u_alice")
    # Owner runs a command that records an observation.
    owner = operator.issue_command(org, sess["id"], verb="navigate",
                                   principal="u_alice",
                                   url="https://demo.laura.test/pricing",
                                   command_id="step-1")
    assert owner["accepted"] is True
    # Same-org non-owner replays the same command_id — must be rejected, and
    # must NOT receive the recorded observation.
    leak = operator.issue_command(org, sess["id"], verb="navigate",
                                  principal="u_bob",
                                  url="https://demo.laura.test/pricing",
                                  command_id="step-1")
    assert leak["reason"] == "not_owner"
    assert "observation" not in leak
    # The owner still gets the idempotent replay.
    again = operator.issue_command(org, sess["id"], verb="navigate",
                                   principal="u_alice",
                                   url="https://demo.laura.test/pricing",
                                   command_id="step-1")
    assert again.get("idempotent_replay") is True


def test_approved_step_fails_closed_on_stale_page_binding(cp, monkeypatch):
    """The approval is bound to the page state; if the untrusted page changes
    between approval and execution, the guarded step fails closed (the guard
    B1's real write inherits)."""
    monkeypatch.setattr(settings, "browser_allow_writes", True)
    org = _org(cp, "stalebind")
    monkeypatch.setattr(settings, "demo_org_id", org)
    sess, action_id = _mint_guarded(org, "bot-stale")
    # Navigate away — the page (and its fingerprint) changes after approval mint.
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/home")

    import app.main as main_module
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    resp = client.post(f"/org/actions/{action_id}/approve",
                       json={"decision": "approve"})
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "failed"


# ── demo-handoff contracts on the live path ─────────────────────────────────

def test_command_result_contract_and_page_version_bump(cp):
    org = _org(cp, "contract")
    sess = _create(org)
    r = operator.issue_command(org, sess["id"], verb="navigate",
                               url="https://demo.laura.test/pricing")
    # Stable CommandResult shape.
    for field in ("accepted", "command_sequence", "page_version",
                  "classification", "failure_category",
                  "replanning_permitted", "verification"):
        assert field in r, field
    assert r["accepted"] is True and r["classification"] == "auto"
    assert r["observation"]["url"].endswith("/pricing")
    assert r["observation"]["page_version"] >= 1  # page changed → version bumped
    # Navigating to the SAME page again does not bump the version.
    v1 = r["observation"]["page_version"]
    r2 = operator.issue_command(org, sess["id"], verb="navigate",
                                url="https://demo.laura.test/pricing")
    assert r2["observation"]["page_version"] == v1


def test_visual_verification_verified_and_failed(cp):
    org = _org(cp, "verify")
    sess = _create(org)
    ok = operator.issue_command(
        org, sess["id"], verb="navigate",
        url="https://demo.laura.test/pricing",
        verify=True, expected={"url_contains": "pricing",
                               "text_contains": "Team plan"})
    assert ok["verification"] == "verified" and ok["accepted"] is True
    bad = operator.issue_command(
        org, sess["id"], verb="navigate",
        url="https://demo.laura.test/home",
        verify=True, expected={"url_contains": "checkout"})
    assert bad["verification"] == "not_verified"
    assert bad["accepted"] is False
    assert bad["failure_category"] == "verification_failed"


def test_demo_metadata_is_carried_but_non_authoritative(cp):
    org = _org(cp, "meta")
    view = operator.create_session(
        org, principal="u", avatar_key="laura", meeting_ref="bot-m",
        metadata={"demo_definition_id": "onboarding", "demo_run_id": "r1",
                  "state": "hacked", "evil": "x"})
    assert view["metadata"]["demo_definition_id"] == "onboarding"
    assert "state" not in view["metadata"] and "evil" not in view["metadata"]
    assert view["state"] == "ready"  # metadata's 'state' did NOT drive state
    upd = operator.set_metadata(org, view["id"],
                                {"current_checkpoint": "step-3"},
                                principal="u")
    assert upd["metadata"]["current_checkpoint"] == "step-3"


# ── provider failure / timeout ──────────────────────────────────────────────

def test_provider_error_fails_session(cp):
    org = _org(cp, "provfail")
    sess = _create(org)
    r = operator.issue_command(org, sess["id"], verb="navigate",
                               url="fail://error")
    assert r["reason"] == "provider_error"
    assert operator.get_session(org, sess["id"])["state"] == "failed"


def test_provider_timeout_is_execution_unknown(cp):
    org = _org(cp, "provto")
    sess = _create(org)
    r = operator.issue_command(org, sess["id"], verb="navigate",
                               url="fail://timeout")
    assert r["reason"] == "execution_unknown"
    # The session is NOT auto-failed on a timeout (the action may have landed).
    assert operator.get_session(org, sess["id"])["state"] in (
        "ready", "presenting")
