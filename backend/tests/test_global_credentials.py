"""A deployment-wide credential must never act for an arbitrary tenant.

Security audit 2026-07-23, gap #2. Two deployment-global credentials were
reachable at EXECUTION time by any org:

  * ``ASANA_TOKEN`` — a Personal Access Token for the deployment owner's own
    Asana. Any org that had not connected Asana silently executed its approved
    task actions inside the owner's workspace, and the dashboard told it it was
    "still connected via env" after disconnecting.
  * ``SLACK_WEBHOOK_URL`` — one incoming webhook, i.e. one workspace.
    ``post_to_slack`` took no org at all, so any tenant's meeting content could
    be published into the deployment owner's Slack.

Both are now confined to the deployment's OWN (demo/key-free) org. A real
tenant must connect its own Asana, and posts to Slack through its org-scoped
Cedric connection. Nothing here contacts a vendor.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.actions import workflow
from app.config import settings
from app.integrations import asana_client

TENANT = "org-real-customer"


@pytest.fixture(autouse=True)
def _no_org_rows(monkeypatch):
    """No org has its own grant — the state in which the fallback used to fire."""
    monkeypatch.setattr(asana_client.store, "get_org_oauth", lambda *a, **k: None)
    monkeypatch.setattr(settings, "asana_token", "deployment-owner-PAT")


# ── Asana ───────────────────────────────────────────────────────────────────

def test_tenant_without_its_own_asana_is_not_connected():
    token, err = asana_client._token(TENANT)
    assert token == "", "a tenant must never receive the deployment owner's PAT"
    assert "not connected" in err


def test_deployments_own_org_may_still_use_the_env_pat():
    """The key-free demo and single-tenant deployments keep working."""
    token, err = asana_client._token(str(settings.demo_org_id))
    assert token == "deployment-owner-PAT" and err == ""
    token, err = asana_client._token("")  # service path, no org resolved
    assert token == "deployment-owner-PAT" and err == ""


def test_env_pat_allowed_is_the_single_rule():
    assert asana_client._env_pat_allowed(str(settings.demo_org_id)) is True
    assert asana_client._env_pat_allowed("") is True
    assert asana_client._env_pat_allowed(TENANT) is False


def test_a_tenants_own_grant_still_wins(monkeypatch):
    monkeypatch.setattr(
        asana_client.store, "get_org_oauth",
        lambda org, provider="asana": (
            {"refresh_token": "tenant-own-PAT"} if provider == "asana" else None
        ),
    )
    token, err = asana_client._token(TENANT)
    assert token == "tenant-own-PAT" and err == ""


# ── Slack webhook ───────────────────────────────────────────────────────────

def test_tenant_cannot_post_through_the_deployment_webhook(monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.example/x")
    sent = []
    monkeypatch.setattr(
        workflow.httpx, "post",
        lambda *a, **k: sent.append(a) or pytest.fail("vendor must not be called"),
    )
    res = workflow.post_to_slack("tenant meeting recap", TENANT)
    assert res["sent"] is False
    assert "Cedric" in res["reason"]
    assert not sent


def test_deployments_own_org_still_posts(monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.example/x")

    class _Resp:
        status_code = 200

    monkeypatch.setattr(workflow.httpx, "post", lambda *a, **k: _Resp())
    assert workflow.post_to_slack("digest", str(settings.demo_org_id))["sent"] is True
    assert workflow.post_to_slack("digest")["sent"] is True  # service path


def test_unconfigured_webhook_is_reported_not_attempted(monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", "")
    res = workflow.post_to_slack("anything")
    assert res["sent"] is False and "SLACK_WEBHOOK_URL" in res["reason"]
