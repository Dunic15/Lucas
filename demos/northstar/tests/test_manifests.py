"""Integrity of the knowledge manifest, demo manifest, and workflow specs; and
that they agree with the product and the knowledge docs."""
from __future__ import annotations

import json
import re

import yaml

from demos.northstar.product import seed
from demos.northstar.product.app import app


def _load_km(demo_dir):
    return json.loads((demo_dir / "knowledge" / "knowledge_manifest.json").read_text())


def _load_dm(demo_dir):
    return json.loads((demo_dir / "demo" / "demo_manifest.json").read_text())


def _headings(md_text: str) -> set[str]:
    return {m.group(1).strip() for m in re.finditer(r"^##\s+(.+)$", md_text, re.M)}


def _app_paths() -> set[str]:
    return {r.path for r in app.routes if hasattr(r, "path")}


# ── knowledge manifest ─────────────────────────────────────────────────────
def test_knowledge_manifest_files_and_headings(demo_dir):
    km = _load_km(demo_dir)
    assert len(km["sources"]) == 12
    ids = set()
    for s in km["sources"]:
        f = demo_dir / s["file"]
        assert f.exists(), s["file"]
        heads = _headings(f.read_text())
        for h in s["headings"]:
            assert h in heads, f"{s['id']}: '{h}' not a heading"
        assert s["primary_citation"] in heads
        ids.add(s["id"])
    assert len(ids) == 12                          # unique ids


def test_knowledge_docs_are_marked_synthetic(demo_dir):
    for s in _load_km(demo_dir)["sources"]:
        assert "SYNTHETIC" in (demo_dir / s["file"]).read_text()


def test_near_duplicate_groups_reference_real_sources(demo_dir):
    km = _load_km(demo_dir)
    ids = {s["id"] for s in km["sources"]}
    assert len(km["near_duplicate_groups"]) >= 3
    for g in km["near_duplicate_groups"]:
        assert len(g["members"]) >= 2
        for m in g["members"]:
            assert m in ids, f"dup group {g['id']} -> unknown {m}"


# ── demo manifest ──────────────────────────────────────────────────────────
REQUIRED_DM_FIELDS = [
    "company_id", "demo_version", "starting_url", "allowed_domains",
    "selected_avatar", "meeting_goal", "ordered_checkpoints",
    "allowed_operations", "guarded_operations", "blocked_operations",
    "max_steps", "max_duration_seconds", "reset", "knowledge_source_mapping",
    "expected_citations", "expected_canonical_action", "expected_receipt",
    "success_criteria", "visual_only_fixture", "health_check",
]


def test_demo_manifest_required_fields(demo_dir):
    dm = _load_dm(demo_dir)
    for k in REQUIRED_DM_FIELDS:
        assert k in dm, f"missing {k}"
    assert dm["company_id"] == seed.COMPANY_ID
    assert dm["demo_version"] == seed.DEMO_VERSION
    assert dm["selected_avatar"] == seed.AVATAR
    assert dm["ordered_checkpoints"] and isinstance(dm["ordered_checkpoints"], list)
    assert isinstance(dm["max_steps"], int) and dm["max_steps"] > 0
    assert isinstance(dm["max_duration_seconds"], int) and dm["max_duration_seconds"] > 0


def test_demo_manifest_citations_valid(demo_dir):
    km = {s["id"]: set(s["headings"]) for s in _load_km(demo_dir)["sources"]}
    for c in _load_dm(demo_dir)["expected_citations"]:
        assert c["source_id"] in km, c["source_id"]
        assert c["heading"] in km[c["source_id"]], c


def test_demo_manifest_knowledge_mapping_valid(demo_dir):
    km_ids = {s["id"] for s in _load_km(demo_dir)["sources"]}
    for m in _load_dm(demo_dir)["knowledge_source_mapping"]:
        for sid in m["sources"]:
            assert sid in km_ids, sid


def test_demo_manifest_guarded_op_matches_product(demo_dir):
    dm = _load_dm(demo_dir)
    op = dm["guarded_operations"][0]
    assert op["id"] == "create_followup_task"
    assert op["path"] == "/api/acme/tasks"
    assert op["path"] in _app_paths()
    assert op["idempotency_key"] == seed.FOLLOWUP_IDEMPOTENCY_KEY
    # allowed read op (preview) also exists on the product
    assert "/api/acme/tasks/preview" in _app_paths()


def test_demo_manifest_receipt_matches_deterministic_create(demo_dir):
    dm = _load_dm(demo_dir)
    assert dm["expected_receipt"] == {
        "status": "created", "task_id": "task-0003",
        "idempotency_key": seed.FOLLOWUP_IDEMPOTENCY_KEY,
        "created_at": seed.DEMO_NOW,
    }
    assert dm["expected_canonical_action"]["idempotency_key"] == seed.FOLLOWUP_IDEMPOTENCY_KEY


def test_demo_manifest_visual_fixture_matches_seed(demo_dir):
    vf = _load_dm(demo_dir)["visual_only_fixture"]
    assert vf["target_stage_id"] == seed.VISUAL_TARGET_STAGE_ID
    assert vf["shared_accessible_label"] == "Onboarding stage"
    assert vf["target_opens"] == "/customers/acme-robotics/onboarding/integration"


def test_starting_url_within_allowed_domains(demo_dir):
    dm = _load_dm(demo_dir)
    assert any(dom in dm["starting_url"] for dom in dm["allowed_domains"])


# ── workflows ──────────────────────────────────────────────────────────────
REQUIRED_WF_KEYS = [
    "id", "name", "purpose", "inputs", "required_company_knowledge",
    "allowed_read_operations", "guarded_operations", "blocked_operations",
    "checkpoints", "expected_receipts", "completion_criteria", "failure_behavior",
]
EXPECTED_WF_IDS = {
    "customer-onboarding-review", "implementation-status-review",
    "blocker-escalation", "followup-task-creation", "go-live-readiness-check",
}


def test_workflows_present_and_valid(demo_dir):
    km_ids = {s["id"] for s in _load_km(demo_dir)["sources"]}
    seen = set()
    for f in sorted((demo_dir / "workflows").glob("*.yaml")):
        wf = yaml.safe_load(f.read_text())
        for k in REQUIRED_WF_KEYS:
            assert k in wf, f"{f.name} missing {k}"
        seen.add(wf["id"])
        for kid in wf["required_company_knowledge"]:
            assert kid in km_ids, f"{f.name} -> unknown knowledge {kid}"
    assert seen == EXPECTED_WF_IDS


def test_guarded_workflow_targets_the_product_endpoint(demo_dir):
    wf = yaml.safe_load(
        (demo_dir / "workflows" / "followup_task_creation.yaml").read_text())
    op = wf["guarded_operations"][0]
    assert op["product_endpoint"] == "POST /api/acme/tasks"
    assert op["idempotency_key"] == seed.FOLLOWUP_IDEMPOTENCY_KEY
    assert "/api/acme/tasks" in _app_paths()
