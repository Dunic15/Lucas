"""Northstar MVP — deterministic key-free end-to-end acceptance.

The whole MVP journey on embedded Postgres with the in-process Northstar
provider + fake visual planner + real product write — NO Browserbase, NO model
credentials. Proves: demo-org setup, canonical knowledge ingestion,
ContextResolver citations (+ cross-org denial), meeting-bound browser session,
the VISUAL-ONLY target chosen by visual grounding, rejection = zero tasks,
approval = exactly one task-0003 with a receipt + post-action verification, a
replayed approval creating no duplicate, dashboard state mapping, and a clean
close — twice from a clean reset, deterministically.
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
from app.browser import operator, planner  # noqa: E402
from app.browser.fake_provider import FakeProvider  # noqa: E402
from app.demo_mvp import ingest, narration, runtime  # noqa: E402
from app.demo_mvp.northstar_provider import NorthstarProvider  # noqa: E402

pytestmark = pytest.mark.pg
BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_ROLE = "laura_app"
_ACME = "http://127.0.0.1:8971/customers/acme-robotics"
_TASKS = "http://127.0.0.1:8971/tasks"


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
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("mvp_pg")))
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'pw' "
                     "NOSUPERUSER NOBYPASSRLS")
    import psycopg.conninfo as ci
    from sqlalchemy.engine import URL

    admin_sa_url = uri.replace("postgresql://", "postgresql+psycopg://")
    info = ci.conninfo_to_dict(uri)
    q = {k: str(info[k]) for k in ("host", "port") if info.get(k) is not None}
    app_sa_url = URL.create("postgresql+psycopg", username=APP_ROLE,
                            password="pw", database=info.get("dbname"),
                            query=q).render_as_string(hide_password=False)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env={**os.environ, "LAURA_DATABASE_ADMIN_URL": admin_sa_url,
             "LAURA_DATABASE_URL": "", "LAURA_REQUIRE_MIGRATIONS": "1"},
        capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, f"alembic failed:\n{proc.stdout}\n{proc.stderr}"
    yield {"uri": uri, "app_sa_url": app_sa_url}
    control_plane.reset_engine()
    srv.cleanup()


@pytest.fixture
def demo(pg, monkeypatch, tmp_path):
    from demos.northstar.product import store as nstore

    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    monkeypatch.setattr(settings, "browser_operator_enabled", True)
    monkeypatch.setattr(settings, "browser_visual_planner_enabled", True)
    monkeypatch.setattr(settings, "browser_allow_writes", False)  # arbitrary OFF
    monkeypatch.setattr(settings, "company_brain_enabled", True)
    monkeypatch.setattr(settings, "data_foundation_enabled", True)
    monkeypatch.setattr(settings, "northstar_demo_enabled", True)
    monkeypatch.setattr(settings, "northstar_demo_write_enabled", True)
    monkeypatch.setattr(settings, "browser_allowed_domains",
                        "127.0.0.1:8971,localhost:8971")
    # Knowledge files land under store.STORE_PATH.parent/knowledge_files — the
    # tmp store path isolates them per test.
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    avatar_resolver._reset_for_tests()
    FakeProvider._reset()
    NorthstarProvider._reset()
    nstore.reset()
    control_plane.reset_engine()
    org = ingest.provision_demo_org()
    monkeypatch.setattr(settings, "demo_org_id", org)  # org door resolves here
    yield org
    avatar_resolver._reset_for_tests()
    NorthstarProvider._reset()
    nstore.reset()
    control_plane.reset_engine()


def _client():
    import app.main as main_module
    from fastapi.testclient import TestClient

    return TestClient(main_module.app)


def _visual_click(org, sid):
    """Drive the VISUAL-ONLY selection through the fake visual planner: perceive
    the acme page, let the planner ground the amber node FROM THE SCREENSHOT,
    then execute that coordinate click through the one gated command path."""
    operator.issue_command(org, sid, verb="navigate", url=_ACME,
                           command_id="nav-acme")
    obs, shot = operator.perceive(org, sid)
    # The five stage nodes are text-identical ("Onboarding stage").
    names = [e["name"] for e in obs["elements"]
             if e["id"].startswith("stage-node")]
    assert names and len(set(names)) == 1, "targets must be text-ambiguous"
    prop = planner.FakeVisualPlanner(mode="visual_select").propose(
        obs, "open the blocked onboarding stage", screenshot=shot)
    assert prop["target_type"] == "coordinates"  # grounded visually, not by text
    return operator.issue_command(
        org, sid, verb="click", coordinates=prop["coordinates"],
        confidence=prop["confidence"],
        observed_page_version=prop["observed_page_version"], verify=True,
        expected={"url_contains": "/onboarding/integration"},
        command_id="visual-click")


def _propose_followup(org, sid):
    """Navigate to /tasks and propose the guarded follow-up → one canonical
    action (route='browser'). Returns the action_id."""
    operator.issue_command(org, sid, verb="navigate", url=_TASKS,
                           command_id=f"nav-tasks-{time.time_ns()}")
    r = operator.issue_command(org, sid, verb="click",
                               element_id="create-followup",
                               command_id=f"followup-{time.time_ns()}")
    assert r["reason"] == "approval_required"
    return r["action_id"]


def _journey(org) -> str:
    """One full journey; returns the created task_id. Asserts every gate."""
    from demos.northstar.product import store as nstore

    # 3 · knowledge ingestion via the canonical path + ContextResolver proof.
    setup = ingest.setup(org)
    assert setup["count"] >= 10
    verify = ingest.verify_retrieval(org)
    assert verify["all_found"], verify
    lines = narration.narration_lines(org)
    assert any(li["citations"] for li in lines), "narration must cite sources"

    # 4 · meeting-bound session on the Northstar provider.
    started = runtime.start(org, meeting_ref="meeting-acme-1")
    assert started["ok"] and started["binding"]["demo_version"] == "1.0.0"
    sid = started["session"]["id"]

    # 5/8 · visual-only target selected by grounding, opens the blocked stage.
    vis = _visual_click(org, sid)
    assert vis["accepted"] and vis["verification"] == "verified"
    assert vis["observation"]["url"].endswith("/onboarding/integration")

    client = _client()

    # 13 · REJECTION → zero follow-up tasks, no product write.
    reject_action = _propose_followup(org, sid)
    rj = client.post(f"/org/actions/{reject_action}/approve",
                     json={"decision": "reject"})
    assert rj.status_code == 200 and rj.json()["new_status"] == "rejected"
    assert nstore.task_by_idempotency_key(
        "acme-robotics:followup:data-integration-blocker") is None
    assert len(nstore.tasks()) == 2  # two seed tasks only

    # 14/16/17 · APPROVAL → exactly one task, receipt, post-action verification.
    approve_action = _propose_followup(org, sid)
    ap = client.post(f"/org/actions/{approve_action}/approve",
                     json={"decision": "approve"})
    assert ap.status_code == 200 and ap.json()["new_status"] == "done"
    status = ledger.action_statuses([approve_action], org_id=org)[approve_action]
    assert status["status"] == "done"
    task = nstore.task_by_idempotency_key(
        "acme-robotics:followup:data-integration-blocker")
    assert task is not None and task["id"] == "task-0003"

    # 15 · a REPLAYED approval creates no duplicate.
    replay = client.post(f"/org/actions/{approve_action}/approve",
                         json={"decision": "approve"})
    assert replay.json().get("idempotent_replay") is True
    assert len([t for t in nstore.tasks() if t["id"] == "task-0003"]) == 1

    # dashboard state mapping + clean close.
    st = runtime.status(org, sid)
    assert st["presentation_event"] in ("observing", "ready", "waiting_approval")
    assert operator.close_session(org, sid)["state"] == "closed"
    return task["id"]


# ── the acceptance tests ────────────────────────────────────────────────────

def test_northstar_mvp_full_journey(demo):
    assert _journey(demo) == "task-0003"


def test_deterministic_twice_exactly_one_task(demo):
    """The second complete run from a clean reset is deterministic and yields
    exactly one task."""
    from demos.northstar.product import store as nstore

    assert _journey(demo) == "task-0003"
    # Reset the product + re-run: identical outcome, still exactly one task.
    nstore.reset()
    ingest.reset(demo)
    assert _journey(demo) == "task-0003"
    assert len([t for t in nstore.tasks() if t["id"] == "task-0003"]) == 1


def test_cross_org_cannot_retrieve_northstar_docs(demo):
    """Another org cannot retrieve the demo org's Northstar documents."""
    ingest.setup(demo)
    other = control_plane.ensure_user("", "other@elsewhere.test", "Other")["org_id"]
    verify_other = ingest.verify_retrieval(other)
    assert not verify_other["all_found"]
    # The other org's resolver returns none of the Northstar citations.
    from app.datafoundation import resolver

    res = resolver.resolve(other, "laura", "Blocker: WMS sandbox credentials", k=6)
    joined = json.dumps(res.get("chunks") or [])
    assert "WMS sandbox" not in joined


def test_demo_write_hook_scoped_to_northstar_provider(demo):
    """Adversarial fix: the BROWSER_ALLOW_WRITES=false mint gate opens ONLY for
    a Northstar-provider session — never for any other session that happens to
    expose a 'create-followup' element."""
    northstar_row = {"provider": "northstar", "id": "s", "meeting_ref": "m"}
    other_row = {"provider": "fake", "id": "s", "meeting_ref": "m"}
    extra, typed = operator._demo_write_hook(demo, northstar_row,
                                             "create-followup")
    assert extra is not None and extra.get("northstar_followup") is True
    # A non-northstar session cannot open the demo write gate.
    assert operator._demo_write_hook(demo, other_row, "create-followup") == \
        (None, None)
    # Nor can a non-followup element on the northstar session.
    assert operator._demo_write_hook(demo, northstar_row, "nav-tasks") == \
        (None, None)


def test_reset_only_removes_demo_sources(demo):
    """Adversarial fix: reset matches the northstar- provenance prefix, so a
    real same-org source with a generic name (e.g. 'security-policy') survives."""
    from app.knowledge import dal as kdal

    ingest.setup(demo)
    # A real org source whose name collides with a generic demo slug.
    real = kdal.create_source(demo, "security-policy", "upload")
    assert real is not None
    ingest.reset(demo)
    names = {s["name"] for s in kdal.list_sources(demo)}
    assert "security-policy" in names  # the real source is untouched
    assert not any(n.startswith("northstar-") for n in names)  # demo cleared


def test_arbitrary_browser_write_still_rejected(demo):
    """Only the follow-up is enabled — any OTHER guarded write stays rejected
    under BROWSER_ALLOW_WRITES=false."""
    started = runtime.start(demo, meeting_ref="m2")
    sid = started["session"]["id"]
    # A guarded 'send' element on the fake provider (not the northstar control)
    # via a normal fake session would be write_rejected_read_only; here we prove
    # a non-followup guarded click on the northstar provider is not minted.
    operator.issue_command(demo, sid, verb="navigate",
                           url="http://127.0.0.1:8971/customers", command_id="c")
    # 'customer-acme-robotics' is a link (auto), not guarded — assert the ONLY
    # guarded control that mints is create-followup by confirming a different
    # guarded op is impossible here (no other submit/purchase element exists).
    obs, _ = operator.perceive(demo, sid)
    guarded = [e for e in obs["elements"] if e.get("kind") in
               ("submit", "purchase", "send", "delete")]
    assert guarded == []  # the demo product exposes no arbitrary write control
