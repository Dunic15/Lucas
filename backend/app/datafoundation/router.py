"""DF HTTP surface (accepted contract v5): /org/data/* + strict dashboard twin.

org_id and principal derive EXCLUSIVELY from the authenticated context; a
client-supplied org_id in any body is rejected on mismatch (403); never
honored. Writes on the dashboard twin require cookie + same-origin + the
durable owner/admin gate; every branch checks its METHOD explicitly. No
business logic lives in the frontend.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import auth
from . import dal, enabled, resolver, sync

router = APIRouter(tags=["data-foundation"])
_NO_STORE = {"Cache-Control": "no-store"}


def _jsonable(value):
    """Coerce Postgres numerics (extract(epoch) -> Decimal, count -> Decimal)
    to JSON-safe types at the response boundary. Starlette's JSONResponse
    uses plain json.dumps and would 500 on a Decimal."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        f = float(value)
        return int(f) if f.is_integer() else f
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _json(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(_jsonable(payload), status_code=status_code,
                        headers=_NO_STORE)


def _disabled() -> JSONResponse:
    return JSONResponse({"error": "data_foundation_disabled"},
                        status_code=404, headers=_NO_STORE)


async def _machine_gate(request: Request):
    from .. import org_api

    return await org_api._machine_gate(request)


def _admin_error(user: dict) -> str:
    from .. import org_avatars_api

    return org_avatars_api._admin_error(user)


def _org_mismatch(body: dict, org: str) -> bool:
    supplied = str((body or {}).get("org_id") or "").strip()
    return bool(supplied) and supplied != org


# ── shared sync ops ─────────────────────────────────────────────────────────

def _op_connectors_get(org: str) -> tuple[int, dict]:
    return 200, {"connectors": dal.list_connectors(org)}


def _op_connector_create(org: str, body: dict, actor: str) -> tuple[int, dict]:
    kind = str((body or {}).get("kind") or "").strip()
    row = dal.create_connector(
        org, kind, str((body or {}).get("name") or kind),
        config=(body or {}).get("config")
        if isinstance((body or {}).get("config"), dict) else None,
        credential_ref=str((body or {}).get("credential_ref") or ""),
        trusted_email_issuer=bool((body or {}).get("trusted_email_issuer")),
        actor=actor,
    )
    if row is None:
        return 400, {"error": "unknown connector kind"}
    return 200, {"ok": True, "connector": row}


def _op_connector_get(org: str, cid: str) -> tuple[int, dict]:
    row = dal.get_connector(org, cid)
    if row is None:
        return 404, {"error": "unknown connector"}
    row.pop("config_json", None)  # config may hold fixture bulk; not for reads
    return 200, {"connector": row}


def _op_connector_sync(org: str, cid: str, body: dict) -> tuple[int, dict]:
    row = dal.get_connector(org, cid)
    if row is None:
        return 404, {"error": "unknown connector"}
    kind = str((body or {}).get("kind") or "incremental")
    run_id = dal.enqueue_run(org, cid, kind)
    return 200, {"ok": True, "run_id": run_id}


def _op_connector_status(org: str, cid: str, body: dict,
                         actor: str) -> tuple[int, dict]:
    status = str((body or {}).get("status") or "")
    if not dal.set_connector_status(org, cid, status, actor):
        return 400, {"error": "unknown connector or status"}
    return 200, {"ok": True, "status": status}


def _op_runs(org: str, cid: str = "") -> tuple[int, dict]:
    return 200, {"runs": dal.run_rows(org, cid)}


def _op_run_retry(org: str, run_id: str) -> tuple[int, dict]:
    try:
        ok = dal.retry_run(org, int(run_id))
    except (TypeError, ValueError):
        return 400, {"error": "bad run id"}
    return (200, {"ok": True}) if ok else (404, {"error": "not retryable"})


def _op_quarantine(org: str) -> tuple[int, dict]:
    return 200, {"quarantine": dal.quarantine_rows(org)}


def _op_quarantine_replay(org: str, qid: str, actor: str) -> tuple[int, dict]:
    row = dal.get_quarantine(org, qid)
    if row is None:
        return 404, {"error": "unknown quarantine item"}
    if row["state"] != "open":
        return 409, {"error": "already resolved"}
    payload = None
    if row["payload_ref"]:
        import json as _json

        from ..knowledge import storage

        raw = storage.get_bytes(row["payload_ref"])
        if raw:
            try:
                payload = _json.loads(raw)
            except ValueError:
                payload = None
    if payload is None:
        return 409, {"error": "payload unavailable"}
    try:
        stats = dal.commit_batch(
            org, row["connector_id"], [payload], new_cursor=None,
            sync_run_id=None,
        )
    except dal.BackpressureError:
        # Replaying while quarantine is at the open cap would otherwise 500.
        return 409, {"error": "quarantine_backpressure"}
    affected = stats.pop("affected_docs", [])
    sync._reconcile_retrieval(org, affected)
    if stats.get("quarantined"):
        return 409, {"error": "envelope still invalid", "stats": stats}
    dal.resolve_quarantine(org, qid, "replayed", actor)
    return 200, {"ok": True, "stats": stats}


def _op_quarantine_discard(org: str, qid: str, actor: str) -> tuple[int, dict]:
    ok = dal.resolve_quarantine(org, qid, "discarded", actor)
    return (200, {"ok": True}) if ok else (404, {"error": "not open"})


def _op_records(org: str, principal_id: str) -> tuple[int, dict]:
    identity_ids, complete = (set(), True)
    if principal_id:
        identity_ids, complete = dal.principal_identity_closure(
            org, principal_id
        )
    heads = dal.visible_heads(org, principal_identity_ids=identity_ids,
                              limit=200)
    for head in heads:
        head.pop("body_ref", None)  # metadata only on this listing
        head.pop("lineage_json", None)
    return 200, {"records": heads,
                 "resolution": {"complete": complete}}


def _op_resolve(org: str, principal_id: str, body: dict) -> tuple[int, dict]:
    query = str((body or {}).get("query") or "").strip()
    avatar_key = str((body or {}).get("avatar_key") or "").strip()
    if not query or not avatar_key:
        return 400, {"error": "avatar_key and query are required"}
    result = resolver.resolve(
        org, avatar_key, query,
        k=int((body or {}).get("k") or 6),
        principal_id=principal_id,
        purpose=str((body or {}).get("purpose") or ""),
        scope=(body or {}).get("scope")
        if isinstance((body or {}).get("scope"), dict) else None,
    )
    return 200, result


def _op_freshness(org: str) -> tuple[int, dict]:
    return 200, {"freshness": dal.freshness(org)}


def _op_bind(org: str, identity_id: str, body: dict,
             actor: str) -> tuple[int, dict]:
    principal = str((body or {}).get("principal_ref") or "").strip()
    if not principal:
        return 400, {"error": "principal_ref is required"}
    ok = dal.bind_identity(org, identity_id, principal, actor)
    return (200, {"ok": True}) if ok else (404, {"error": "unknown identity"})


def _op_retention(org: str) -> tuple[int, dict]:
    purged_q = dal.purge_quarantine(org)
    purged_v = dal.purge_record_versions(org)
    completed = sync.complete_purge_audits(org)
    return 200, {"ok": True, "quarantine_purged": purged_q,
                 "versions_purged": purged_v, "audits_completed": completed}


def _op_backfill(org: str) -> tuple[int, dict]:
    """Rollout step 3: upload backfill (binding deployment order)."""
    connector = dal.ensure_connector(org, "upload", "Company Brain uploads")
    run_id = dal.enqueue_run(org, connector["id"], "full")
    return 200, {"ok": True, "connector_id": connector["id"],
                 "run_id": run_id}


# ── machine surface: /org/data/* ────────────────────────────────────────────

async def _machine(request: Request, handler, *args,
                   needs_body: bool = False) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _machine_gate(request)
    if err:
        return err
    body: dict = {}
    if needs_body:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if _org_mismatch(body, org):
            return JSONResponse({"error": "org mismatch"}, status_code=403)
        args = (*args, body)
    code, payload = await run_in_threadpool(handler, org, *args)
    return _json(payload, code)


@router.get("/org/data/connectors")
async def org_df_connectors(request: Request) -> JSONResponse:
    return await _machine(request, _op_connectors_get)


@router.post("/org/data/connectors")
async def org_df_connector_create(request: Request) -> JSONResponse:
    return await _machine(
        request, lambda org, body: _op_connector_create(org, body, "machine"),
        needs_body=True,
    )


@router.get("/org/data/connectors/{cid}")
async def org_df_connector(cid: str, request: Request) -> JSONResponse:
    return await _machine(request, _op_connector_get, cid)


@router.post("/org/data/connectors/{cid}/sync")
async def org_df_connector_sync(cid: str, request: Request) -> JSONResponse:
    return await _machine(
        request, lambda org, body: _op_connector_sync(org, cid, body),
        needs_body=True,
    )


@router.get("/org/data/runs")
async def org_df_runs(request: Request) -> JSONResponse:
    return await _machine(request, _op_runs)


@router.post("/org/data/runs/{run_id}/retry")
async def org_df_run_retry(run_id: str, request: Request) -> JSONResponse:
    return await _machine(request, _op_run_retry, run_id)


@router.get("/org/data/quarantine")
async def org_df_quarantine(request: Request) -> JSONResponse:
    return await _machine(request, _op_quarantine)


@router.post("/org/data/quarantine/{qid}/replay")
async def org_df_quarantine_replay(qid: str, request: Request) -> JSONResponse:
    return await _machine(
        request, lambda org: _op_quarantine_replay(org, qid, "machine")
    )


@router.get("/org/data/records")
async def org_df_records(request: Request) -> JSONResponse:
    # Machine callers carry org authority, not a human principal: records
    # listing is org_default-visible content only (fail-closed for identities).
    return await _machine(request, _op_records, "")


@router.post("/org/data/resolve")
async def org_df_resolve(request: Request) -> JSONResponse:
    # Machine bearer = org authority, no human principal: resolves see
    # org_default content only (fail-closed).
    return await _machine(request, lambda org, body: _op_resolve(org, "", body),
                          needs_body=True)


@router.get("/org/data/freshness")
async def org_df_freshness(request: Request) -> JSONResponse:
    return await _machine(request, _op_freshness)


# ── dashboard twin (cookie + same-origin; admin writes; strict methods) ────

@router.api_route("/dashboard/data/{tail:path}",
                  methods=["GET", "POST", "DELETE"])
async def dashboard_data(tail: str, request: Request) -> JSONResponse:
    if not enabled():
        return _disabled()
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    org = str(user.get("org_id") or "")
    principal = str(user.get("user_id") or "")
    method = request.method
    if method != "GET":
        if not auth._same_origin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        admin_err = _admin_error(user)
        if admin_err:
            return JSONResponse({"error": admin_err}, status_code=403)
    body: dict = {}
    if method == "POST":
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if _org_mismatch(body, org):
            return JSONResponse({"error": "org mismatch"}, status_code=403)
    parts = [p for p in (tail or "").split("/") if p]

    def run() -> tuple[int, dict]:
        if parts == ["connectors"] and method == "GET":
            return _op_connectors_get(org)
        if parts == ["connectors"] and method == "POST":
            return _op_connector_create(org, body, principal)
        if len(parts) == 2 and parts[0] == "connectors" and method == "GET":
            return _op_connector_get(org, parts[1])
        if (len(parts) == 3 and parts[0] == "connectors"
                and parts[2] == "sync" and method == "POST"):
            return _op_connector_sync(org, parts[1], body)
        if (len(parts) == 3 and parts[0] == "connectors"
                and parts[2] == "status" and method == "POST"):
            return _op_connector_status(org, parts[1], body, principal)
        if parts == ["runs"] and method == "GET":
            return _op_runs(org)
        if (len(parts) == 3 and parts[0] == "runs" and parts[2] == "retry"
                and method == "POST"):
            return _op_run_retry(org, parts[1])
        if parts == ["quarantine"] and method == "GET":
            return _op_quarantine(org)
        if (len(parts) == 3 and parts[0] == "quarantine"
                and parts[2] == "replay" and method == "POST"):
            return _op_quarantine_replay(org, parts[1], principal)
        if (len(parts) == 3 and parts[0] == "quarantine"
                and parts[2] == "discard" and method == "POST"):
            return _op_quarantine_discard(org, parts[1], principal)
        if parts == ["records"] and method == "GET":
            return _op_records(org, principal)
        if parts == ["resolve"] and method == "POST":
            return _op_resolve(org, principal, body)
        if parts == ["freshness"] and method == "GET":
            return _op_freshness(org)
        if (len(parts) == 3 and parts[0] == "identities"
                and parts[2] == "bind" and method == "POST"):
            return _op_bind(org, parts[1], body, principal)
        if parts == ["retention", "run"] and method == "POST":
            return _op_retention(org)
        if parts == ["backfill"] and method == "POST":
            return _op_backfill(org)
        if parts == ["debug", "acl-stats"] and method == "GET":
            # ADMIN-ONLY even for reads: inaccessible-record volume must not
            # leak to ordinary members (contract non-disclosure rule).
            admin_err = _admin_error(user)
            if admin_err:
                return 403, {"error": admin_err}
            return 200, {"acl_stats": resolver.acl_stats(org)}
        return 404, {"error": "unknown data route"}

    code, payload = await run_in_threadpool(run)
    return _json(payload, code)

