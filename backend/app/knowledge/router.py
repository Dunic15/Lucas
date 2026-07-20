"""Company Brain HTTP surface — /org/knowledge/* (machine) + dashboard twins.

Machine routes sit behind the same per-org bearer gate as the rest of /org/*;
dashboard routes behind the login cookie + same-origin, org-scoped
server-side. Every route 404s cleanly when the feature flag is off so the
key-free demo and existing deployments never see a new surface by accident.

DISTILLED DATA ONLY leaves this API: source/document metadata, bounded chunk
excerpts with citations — raw files stay in object storage, and nothing here
ever touches transcripts.
"""
from __future__ import annotations

import base64
import hashlib

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import auth, avatars, rag
from ..config import settings
from . import dal, enabled, storage

router = APIRouter(tags=["knowledge"])

_NO_STORE = {"Cache-Control": "no-store"}


def _disabled() -> JSONResponse:
    return JSONResponse(
        {"error": "company_brain_disabled"}, status_code=404,
        headers=_NO_STORE,
    )


async def _org_gate(request: Request) -> tuple[JSONResponse | None, str]:
    from .. import org_api

    return await org_api._machine_gate(request)


def _dash_org(request: Request) -> tuple[JSONResponse | None, str]:
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err, ""
        return (
            JSONResponse({"error": "login required"}, status_code=401), ""
        )
    if request.method != "GET" and not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403), ""
    return None, user["org_id"]


# ── shared sync implementations (org + dashboard doors call these) ──────────

def _create_source(org: str, body: dict) -> tuple[int, dict]:
    name = str((body or {}).get("name") or "").strip()
    kind = str((body or {}).get("kind") or "upload").strip()
    folder = str((body or {}).get("drive_folder_id") or "").strip()
    if kind not in ("upload", "drive"):
        return 400, {"error": "kind must be upload|drive"}
    if kind == "drive" and not folder:
        return 400, {"error": "drive sources need drive_folder_id"}
    source = dal.create_source(org, name, kind, folder)
    if source is None:
        return 400, {"error": "could not create source"}
    return 200, {"ok": True, "source": source}


def _upload_document(org: str, source_id: str, body: dict) -> tuple[int, dict]:
    filename = str((body or {}).get("filename") or "").strip()
    if not filename:
        return 400, {"error": "filename is required"}
    text = (body or {}).get("text")
    content_b64 = (body or {}).get("content_base64")
    # Enforce the cap on the ENCODED payload, before any decode: b64decode
    # (and .encode()) allocate the full output buffer, so checking len(data)
    # afterwards would admit an arbitrary-size allocation first — an OOM
    # hazard in the same process that runs live meetings. 4/3 is base64's
    # exact expansion; +8 covers padding/newlines slack.
    max_encoded = settings.knowledge_max_file_bytes * 4 // 3 + 8
    if isinstance(text, str) and text.strip():
        if len(text) > settings.knowledge_max_file_bytes:
            return 413, {"error": "file too large",
                         "max_bytes": settings.knowledge_max_file_bytes}
        data = text.encode()
    elif isinstance(content_b64, str) and content_b64.strip():
        if len(content_b64) > max_encoded:
            return 413, {"error": "file too large",
                         "max_bytes": settings.knowledge_max_file_bytes}
        try:
            data = base64.b64decode(content_b64, validate=True)
        except Exception:  # noqa: BLE001
            return 400, {"error": "content_base64 is not valid base64"}
    else:
        return 400, {"error": "text or content_base64 is required"}
    if len(data) > settings.knowledge_max_file_bytes:
        return 413, {"error": "file too large",
                     "max_bytes": settings.knowledge_max_file_bytes}
    doc = dal.upsert_document(
        org, source_id, filename,
        mime=str((body or {}).get("mime") or "")[:100],
        size_bytes=len(data),
        checksum=hashlib.sha256(data).hexdigest(),
    )
    if doc is None:
        return 404, {"error": "unknown source"}
    ref = storage.put_bytes(org, doc["id"], filename, data)
    dal.set_document_storage(org, doc["id"], ref)
    dal.enqueue_job(org, source_id, "ingest_document", doc["id"])
    return 200, {"ok": True, "document_id": doc["id"], "queued": True}


def _test_search(org: str, body: dict) -> tuple[int, dict]:
    q = str((body or {}).get("q") or "").strip()
    if not q:
        return 400, {"error": "q is required"}
    avatar_id = str(
        (body or {}).get("avatar_id") or settings.default_avatar_id or "laura"
    ).strip()
    semantic: list[dict] = []
    try:
        avatar = avatars.load(avatar_id)
        semantic = [
            {"text": r.text[:800], "source": r.source, "section": r.section,
             "score": round(r.score, 4)}
            for r in rag.retrieve(avatar, q, k=6, org_id=org)
        ]
    except Exception:  # noqa: BLE001 — an unknown avatar id keeps keyword hits
        pass
    keyword = dal.keyword_search(org, q, avatar_id=avatar_id, limit=6)
    return 200, {
        "query": q, "avatar_id": avatar_id,
        "semantic": semantic, "keyword": keyword,
    }


# ── machine surface: /org/knowledge/* ───────────────────────────────────────

@router.post("/org/knowledge/sources")
async def org_create_source(request: Request) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    code, payload = await run_in_threadpool(_create_source, org, body)
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.get("/org/knowledge/sources")
async def org_list_sources(request: Request) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    sources = await run_in_threadpool(dal.list_sources, org)
    return JSONResponse({"sources": sources}, headers=_NO_STORE)


@router.delete("/org/knowledge/sources/{source_id}")
async def org_delete_source(source_id: str, request: Request) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    ok = await run_in_threadpool(dal.delete_source, org, source_id)
    if not ok:
        return JSONResponse({"error": "unknown source"}, status_code=404)
    return JSONResponse({"ok": True, "deleted": source_id}, headers=_NO_STORE)


@router.post("/org/knowledge/sources/{source_id}/sync")
async def org_sync_source(source_id: str, request: Request) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    source = await run_in_threadpool(dal.get_source, org, source_id)
    if source is None:
        return JSONResponse({"error": "unknown source"}, status_code=404)
    kind = "sync_drive" if source["kind"] == "drive" else "rebuild_index"
    await run_in_threadpool(dal.enqueue_job, org, source_id, kind)
    return JSONResponse({"ok": True, "queued": kind}, headers=_NO_STORE)


@router.post("/org/knowledge/sources/{source_id}/documents")
async def org_upload_document(source_id: str, request: Request) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    code, payload = await run_in_threadpool(
        _upload_document, org, source_id, body
    )
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.get("/org/knowledge/sources/{source_id}/documents")
async def org_list_documents(source_id: str, request: Request) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    docs = await run_in_threadpool(dal.list_documents, org, source_id)
    return JSONResponse({"documents": docs}, headers=_NO_STORE)


@router.post("/org/knowledge/sources/{source_id}/assign")
async def org_assign(source_id: str, request: Request) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    avatar_id = str((body or {}).get("avatar_id") or "").strip()
    if not avatar_id:
        return JSONResponse({"error": "avatar_id is required"}, status_code=400)
    ok = await run_in_threadpool(dal.assign, org, source_id, avatar_id)
    if not ok:
        return JSONResponse({"error": "unknown source"}, status_code=404)
    return JSONResponse({"ok": True}, headers=_NO_STORE)


@router.delete("/org/knowledge/sources/{source_id}/assign/{avatar_id}")
async def org_unassign(
    source_id: str, avatar_id: str, request: Request
) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    ok = await run_in_threadpool(dal.unassign, org, source_id, avatar_id)
    return JSONResponse({"ok": bool(ok)}, headers=_NO_STORE)


@router.post("/org/knowledge/search")
async def org_test_search(request: Request) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    code, payload = await run_in_threadpool(_test_search, org, body)
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.get("/org/knowledge/jobs")
async def org_jobs(request: Request) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    jobs = await run_in_threadpool(dal.job_rows, org)
    return JSONResponse({"jobs": jobs}, headers=_NO_STORE)


# ── dashboard twins (login cookie; the Brain Sources section calls these) ──

@router.api_route(
    "/dashboard/knowledge/{tail:path}", methods=["GET", "POST", "DELETE"]
)
async def dashboard_knowledge(tail: str, request: Request) -> JSONResponse:
    """One dashboard door mirroring the /org/knowledge tree with cookie auth:
    the tail is interpreted exactly like the machine routes above."""
    if not enabled():
        return _disabled()
    err, org = _dash_org(request)
    if err:
        return err
    parts = [p for p in (tail or "").split("/") if p]
    body: dict = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    def run() -> tuple[int, dict]:
        # Every branch checks the METHOD explicitly: a mutating route must
        # never fire on GET (GET is exempt from the same-origin check, so a
        # method-lenient mutating branch would be a CSRF-shaped hole).
        if parts == ["sources"] and request.method == "POST":
            return _create_source(org, body)
        if parts == ["sources"] and request.method == "GET":
            return 200, {"sources": dal.list_sources(org)}
        if len(parts) == 2 and parts[0] == "sources" and request.method == "DELETE":
            ok = dal.delete_source(org, parts[1])
            return (200, {"ok": True}) if ok else (404, {"error": "unknown source"})
        if len(parts) == 3 and parts[0] == "sources" and parts[2] == "documents":
            if request.method == "POST":
                return _upload_document(org, parts[1], body)
            if request.method == "GET":
                return 200, {"documents": dal.list_documents(org, parts[1])}
            return 405, {"error": "method not allowed"}
        if (len(parts) == 3 and parts[0] == "sources" and parts[2] == "sync"
                and request.method == "POST"):
            source = dal.get_source(org, parts[1])
            if source is None:
                return 404, {"error": "unknown source"}
            kind = "sync_drive" if source["kind"] == "drive" else "rebuild_index"
            dal.enqueue_job(org, parts[1], kind)
            return 200, {"ok": True, "queued": kind}
        if (len(parts) == 3 and parts[0] == "sources" and parts[2] == "assign"
                and request.method == "POST"):
            avatar_id = str(body.get("avatar_id") or "").strip()
            if not avatar_id:
                return 400, {"error": "avatar_id is required"}
            ok = dal.assign(org, parts[1], avatar_id)
            return (200, {"ok": True}) if ok else (404, {"error": "unknown source"})
        if (len(parts) == 4 and parts[0] == "sources" and parts[2] == "assign"
                and request.method == "DELETE"):
            return 200, {"ok": bool(dal.unassign(org, parts[1], parts[3]))}
        if parts == ["search"] and request.method == "POST":
            return _test_search(org, body)
        if parts == ["jobs"] and request.method == "GET":
            return 200, {"jobs": dal.job_rows(org)}
        return 404, {"error": "unknown knowledge route"}

    code, payload = await run_in_threadpool(run)
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)
