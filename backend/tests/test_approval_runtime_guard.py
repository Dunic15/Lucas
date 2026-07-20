"""Regression tests for the Laura-owned approval execution boundary."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.testclient import TestClient

import app.main as main_module  # noqa: F401 - constructs the patched production app
from app import approval_runtime_guard, executor, org_api
from app.cedric import callback
from app.config import settings


def test_approval_request_blocks_external_cedric_dispatch(monkeypatch):
    app = FastAPI()
    approval_runtime_guard.install(app)

    @app.post("/org/actions/{action_id}/approve")
    async def approve(action_id: str):
        return await run_in_threadpool(
            callback.dispatch_action,
            "org-a",
            {"action_id": action_id, "item": "Unsupported freeform task"},
        )

    # The original callback would attempt a network call when configured.  The
    # approval context must return before it reaches that client.
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric.invalid/api/laura/orgs")
    monkeypatch.setattr(
        callback,
        "_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network dispatch")),
    )

    response = TestClient(app).post("/org/actions/a1/approve")
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "laura_native_only"}


def test_historical_cedric_route_uses_laura_native_runtime(monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    calls: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(
        org_api.ledger,
        "claim_action_execution",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        org_api.store,
        "get_avatar_capabilities",
        lambda avatar_id: {},
    )
    from app import avatar_resolver

    monkeypatch.setattr(
        avatar_resolver,
        "family_allowed",
        lambda org, avatar, family: True,
    )
    monkeypatch.setattr(
        executor,
        "execute_approved",
        lambda org, aid, action: calls.append((org, aid, action))
        or {"ok": True, "kind": "email", "ref": "m1"},
    )

    job_id, status, blocked = org_api._execute_route(
        "org-a",
        "a1",
        {
            "action_id": "a1",
            "execution_route": "cedric",
            "typed": {
                "type": "email.send",
                "args": {
                    "to": ["duccio.profeti@gmail.com"],
                    "subject": "Follow-up",
                    "body": "Notes",
                },
            },
        },
        "laura",
        idempotency_key="exec:a1",
        via="slack",
    )

    assert job_id
    assert status == "done"
    assert blocked is False
    assert len(calls) == 1
    assert calls[0][2]["type"] == "email.send"
