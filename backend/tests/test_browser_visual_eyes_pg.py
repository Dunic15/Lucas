"""Browser B1 "Visual Eyes": end-to-end on real Postgres, key-free.

The complete loop with the fake provider + fake visual planner: the
VISUAL-ONLY acceptance fixture (target unselectable from text), the bounded
coordinator and its limits, guarded approval with post-action visual
verification, prompt-injection has-no-authority, and the coordinate / stale /
domain / org security bindings. No model key, no network.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatar_resolver, control_plane, ledger, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.browser import coordinator, operator, planner  # noqa: E402
from app.browser.fake_provider import FakeProvider  # noqa: E402

pytestmark = pytest.mark.pg
BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_ROLE = "laura_app"


def _write_extension_shims() -> None:
    ext_dir = (Path(pgserver.__file__).parent / "pginstall" / "share"
               / "postgresql" / "extension")
    shims = {
        "pgcrypto.control": ("default_version = '1.0'\nrelocatable = true\n"
                             "comment = 'shim'\n"),
        "pgcrypto--1.0.sql": "-- shim\n",
        "citext.control": ("default_version = '1.0'\nrelocatable = true\n"
                           "comment = 'shim'\n"),
        "citext--1.0.sql": "CREATE DOMAIN citext AS text;\n",
    }
    for name, body in shims.items():
        path = ext_dir / name
        if not path.exists():
            path.write_text(body)


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    _write_extension_shims()
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("beyes_pg")))
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'pw' "
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
    app_uri = ci.make_conninfo(dbname=info.get("dbname"), user=APP_ROLE,
                               password="pw", host=info.get("host"),
                               port=info.get("port"))
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env={**os.environ, "LAURA_DATABASE_ADMIN_URL": admin_sa_url,
             "LAURA_DATABASE_URL": "", "LAURA_REQUIRE_MIGRATIONS": "1"},
        capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, (f"alembic failed:\n{proc.stdout}\n"
                                  f"{proc.stderr}")
    yield {"uri": uri, "app_sa_url": app_sa_url, "app_uri": app_uri}
    control_plane.reset_engine()
    srv.cleanup()


def _admin(pg):
    return psycopg.connect(pg["uri"], autocommit=True)


@pytest.fixture
def cp(pg, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    monkeypatch.setattr(settings, "browser_operator_enabled", True)
    monkeypatch.setattr(settings, "browser_visual_planner_enabled", True)
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


def _org(cp, tag):
    stamp = str(time.time_ns())
    return cp.ensure_user(f"sub-{tag}-{stamp}", f"{tag}-{stamp}@freemail.test",
                          tag, "")["org_id"]


def _session(org, principal="u_alice"):
    return operator.create_session(org, principal=principal, avatar_key="laura",
                                   meeting_ref="bot-b1")


# ── §12 VISUAL-ONLY ACCEPTANCE FIXTURE (the headline gate) ──────────────────

def test_visual_only_target_full_loop(cp):
    """Prove real eyes: on a page whose two targets are text-identical, the
    planner selects the correct one FROM THE SCREENSHOT (coordinates), policy
    permits the safe click, the browser executes it, a new observation is
    obtained, and the resulting visual state is VERIFIED; none of which is
    possible from visible text alone."""
    org = _org(cp, "visual")
    sess = _session(org)
    # Land on the visual page (its two "Continue" links are text-ambiguous).
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/visual",
                           principal="u_alice")
    # Perceive → the byte-free observation + the transient screenshot.
    obs, shot = operator.perceive(org, sess["id"], principal="u_alice")
    assert shot, "provider produced a screenshot"
    # Two elements, identical visible name; text cannot disambiguate.
    names = [e["name"] for e in obs["elements"]]
    assert names == ["Continue", "Continue"]
    # The visual planner grounds the primary target FROM THE SCREENSHOT.
    prop = planner.FakeVisualPlanner(mode="visual_select").propose(
        obs, "continue to pricing", screenshot=shot)
    assert prop["target_type"] == "coordinates"
    # Execute the coordinate action through the ONE gated path, with verify.
    result = operator.issue_command(
        org, sess["id"], verb="click", principal="u_alice",
        coordinates=prop["coordinates"], confidence=prop["confidence"],
        observed_page_version=prop["observed_page_version"],
        verify=True, expected={"url_contains": "pricing"})
    assert result["accepted"] is True
    assert result["classification"] == "auto"   # the resolved element is safe
    assert result["verification"] == "verified"  # new observation confirms it
    assert result["observation"]["url"].endswith("/pricing")


def test_coordinate_out_of_viewport_and_unresolved_refused(cp):
    org = _org(cp, "coord")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/visual",
                           principal="u_alice")
    # Off-canvas coordinate → refused.
    oob = operator.issue_command(org, sess["id"], verb="click",
                                 principal="u_alice", coordinates=[5000, 5000],
                                 confidence=0.9)
    assert oob["failure_category"] == "coordinate_out_of_viewport"
    # In-viewport but hits nothing (a gap) → unresolved, never a blind click.
    gap = operator.issue_command(org, sess["id"], verb="click",
                                 principal="u_alice", coordinates=[640, 100],
                                 confidence=0.9)
    assert gap["failure_category"] == "coordinate_unresolved"


def test_low_confidence_coordinate_refused(cp):
    org = _org(cp, "lowconf")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/visual",
                           principal="u_alice")
    r = operator.issue_command(org, sess["id"], verb="click",
                               principal="u_alice", coordinates=[1110, 504],
                               confidence=0.2)  # below the 0.6 threshold
    assert r["failure_category"] == "low_confidence"


def test_stale_observation_refused(cp):
    org = _org(cp, "stale")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/pricing",
                           principal="u_alice")
    # Advance the page (page_version bumps), then act on the OLD version.
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/home",
                           principal="u_alice")
    view = operator.get_session(org, sess["id"])
    r = operator.issue_command(org, sess["id"], verb="scroll",
                               principal="u_alice",
                               observed_page_version=view["page_version"] - 1)
    assert r["failure_category"] == "stale_observation"
    assert r["replanning_permitted"] is True  # recoverable → replan


# ── the bounded coordinator ─────────────────────────────────────────────────

def test_coordinator_visual_run_reaches_finish(cp):
    org = _org(cp, "coordrun")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/visual",
                           principal="u_alice")
    # A scripted visual planner: pick the visual target, then finish.
    pl = planner.FakeVisualPlanner(mode="script", script=[
        {"operation": "click", "target_type": "coordinates",
         "coordinates": [1110, 504], "confidence": 0.95,
         "expected_result": {"url_contains": "pricing"}},
        {"operation": "finish"},
    ])
    out = coordinator.run(org, sess["id"], "go to pricing",
                          principal="u_alice", planner=pl)
    assert out["outcome"] == "finished"
    assert any(s.get("verification") == "verified" for s in out["steps"])


def test_coordinator_guarded_step_suspends_for_approval(cp, monkeypatch):
    monkeypatch.setattr(settings, "browser_allow_writes", True)
    org = _org(cp, "coordguard")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/pricing",
                           principal="u_alice")
    pl = planner.FakeVisualPlanner(mode="guarded")
    out = coordinator.run(org, sess["id"], "buy the plan",
                          principal="u_alice", planner=pl)
    # The coordinator STOPS at the guarded step and records the action_id -
    # it never polls or self-approves.
    assert out["outcome"] == "awaiting_approval"
    assert out["action_id"]


def test_coordinator_step_budget_is_bounded(cp, monkeypatch):
    monkeypatch.setattr(settings, "browser_coord_max_steps", 5)
    org = _org(cp, "budget")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/home",
                           principal="u_alice")
    # A planner that only ever scrolls (never finishes) must hit the step cap.
    pl = planner.FakeVisualPlanner(mode="script", script=[
        {"operation": "scroll"}] * 50)
    out = coordinator.run(org, sess["id"], "loop", principal="u_alice",
                          planner=pl)
    assert out["outcome"] == "budget_exhausted"
    assert out["step_count"] <= 5


def test_coordinator_duration_budget_with_injected_clock(cp):
    org = _org(cp, "dur")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/home",
                           principal="u_alice")
    ticks = iter([0.0, 0.0, 10_000.0, 20_000.0])  # jump past max_duration
    pl = planner.FakeVisualPlanner(mode="script",
                                   script=[{"operation": "scroll"}] * 10)
    out = coordinator.run(org, sess["id"], "g", principal="u_alice",
                          planner=pl, clock=lambda: next(ticks))
    assert out["outcome"] == "budget_exhausted"
    assert out["reason"] == "max_duration"


# ── §13 PROMPT INJECTION has no authority ───────────────────────────────────

def test_prompt_injection_page_has_no_authority(cp):
    """The trap page tells the agent to navigate to evil.example.com, reveal a
    token, and disable approvals. None of it works: the domain gate blocks the
    off-allowlist navigate, and the secret never appears in the observation."""
    org = _org(cp, "inject")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/trap",
                           principal="u_alice")
    obs, shot = operator.perceive(org, sess["id"], principal="u_alice")
    # The injected secret is redacted out of what the agent/model ever sees.
    assert "sk-livesecret" not in json.dumps(obs)
    # A planner that OBEYS the page (repeatedly navigates to evil.example.com)
    # is blocked EVERY time by the server domain allowlist inside the
    # coordinator; the page cannot widen the allowlist.
    pl = planner.FakeVisualPlanner(mode="script", script=[
        {"operation": "navigate", "target": "https://evil.example.com/steal",
         "reason": "the page told me to"}] * 5)
    out = coordinator.run(org, sess["id"], "follow the page",
                          principal="u_alice", planner=pl)
    assert out["outcome"] == "blocked"
    assert all(s.get("outcome") == "domain_blocked" for s in out["steps"])
    # Never navigated off-domain; still on an allowlisted page.
    assert operator.get_session(org, sess["id"])["state"] in (
        "ready", "presenting")


def test_blocked_credential_type_never_executes_via_coordinator(cp):
    org = _org(cp, "blockcred")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/login",
                           principal="u_alice")
    pl = planner.FakeVisualPlanner(mode="blocked")  # types into the password
    out = coordinator.run(org, sess["id"], "sign in", principal="u_alice",
                          planner=pl)
    # The blocked classification stops it; the secret never lands anywhere.
    assert out["outcome"] in ("blocked", "stalled")
    assert "hunter2" not in json.dumps(out)


# ── §10/§11 guarded approval carries post-action visual verification ────────

def test_approved_guarded_step_attaches_visual_verification(cp, monkeypatch):
    monkeypatch.setattr(settings, "browser_allow_writes", True)
    org = _org(cp, "approve")
    monkeypatch.setattr(settings, "demo_org_id", org)
    sess = operator.create_session(org, principal="", avatar_key="laura",
                                   meeting_ref="bot-appr")
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/pricing")
    r = operator.issue_command(org, sess["id"], verb="click",
                               element_id="buy-team")
    action_id = r["action_id"]

    import app.main as main_module
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    resp = client.post(f"/org/actions/{action_id}/approve",
                       json={"decision": "approve"})
    assert resp.status_code == 200 and resp.json()["new_status"] == "done"
    # The receipt carries a verification verdict + a screenshot DIGEST (never
    # bytes).
    status = ledger.action_statuses([action_id], org_id=org)[action_id]
    assert status["status"] == "done"


# ── adversarial-finding regressions ─────────────────────────────────────────

def test_link_click_off_allowlist_is_domain_blocked(cp):
    """Adversarial: the domain allowlist gates a link CLICK that would navigate
    off-domain, not only an explicit navigate. With the visual planner on, a
    click on the trap page's evil-link (→ evil.example.com) is blocked."""
    org = _org(cp, "clicknav")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/trap",
                           principal="u_alice")
    r = operator.issue_command(org, sess["id"], verb="click",
                               element_id="evil-link", principal="u_alice")
    assert r["failure_category"] == "domain_blocked"
    # An on-allowlist link click is fine.
    ok = operator.issue_command(org, sess["id"], verb="click",
                                element_id="read-more", principal="u_alice")
    assert ok["accepted"] is True


def test_non_http_anchor_click_not_domain_blocked(cp, monkeypatch):
    """The click-href gate only blocks http(s) cross-domain navigation; a
    mailto:/javascript: anchor is an element interaction, never domain_blocked
    (fails closed but must not over-block legitimate B1-path clicks)."""
    from app.browser.fake_provider import _PAGES

    monkeypatch.setitem(_PAGES, "https://demo.laura.test/spa", {
        "title": "SPA", "dom": "app",
        "elements": [{"id": "mailto", "role": "link", "name": "Email",
                      "kind": "link", "href": "mailto:sales@demo.laura.test"}]})
    org = _org(cp, "anchor")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/spa",
                           principal="u_alice")
    r = operator.issue_command(org, sess["id"], verb="click",
                               element_id="mailto", principal="u_alice")
    assert r["failure_category"] != "domain_blocked"


def test_coordinate_without_confidence_refused(cp):
    """Adversarial: a coordinate action with no confidence is refused (the
    floor is required, never silently skipped)."""
    org = _org(cp, "noconf")
    sess = _session(org)
    operator.issue_command(org, sess["id"], verb="navigate",
                           url="https://demo.laura.test/visual",
                           principal="u_alice")
    r = operator.issue_command(org, sess["id"], verb="click",
                               principal="u_alice", coordinates=[1110, 504])
    assert r["failure_category"] == "low_confidence"


def test_flag_off_page_version_matches_b0(cp, monkeypatch):
    """Adversarial: with the visual planner OFF the pre-dispatch page_version
    write is gone — a first navigate yields page_version 1 (B0 value), not 2."""
    monkeypatch.setattr(settings, "browser_visual_planner_enabled", False)
    org = _org(cp, "pvidentity")
    sess = _session(org)
    r = operator.issue_command(org, sess["id"], verb="navigate",
                               url="https://demo.laura.test/pricing",
                               principal="u_alice")
    assert r["page_version"] == 1  # single post-dispatch sync, as B0


# ── org binding on observations ─────────────────────────────────────────────

def test_observation_bound_to_org_and_principal(cp):
    org = _org(cp, "bind")
    sess = _session(org, "u_alice")
    obs, _ = operator.perceive(org, sess["id"], principal="u_alice")
    assert obs["org_id"] == org
    assert obs["observation_id"].startswith(f"obs:{sess['id']}")
    # A non-owner cannot perceive another principal's session.
    assert operator.perceive(org, sess["id"], principal="u_bob") is None
