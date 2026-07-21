"""The Northstar demo product; an isolated FastAPI app.

Self-contained: its own `app` object, never mounted into Laura's backend, no
database, no external service, no auth. Everything it serves is derived from the
frozen seed and is fully resettable. Tests drive it in-process via TestClient,
so the whole suite is network-free.

Routes (predictable, referenced by the demo manifest and tests):
  GET  /                                             Home
  GET  /customers                                    Customers
  GET  /customers/acme-robotics                      Acme account (+ visual fixture)
  GET  /customers/acme-robotics/onboarding           Onboarding checklist (accessible)
  GET  /customers/acme-robotics/onboarding/{stage}   Stage detail
  GET  /customers/acme-robotics/implementation       Implementation status
  GET  /tasks                                         Tasks (guarded op UI)
  GET  /security                                      Security settings
  GET  /healthz                                       Health check
  POST /admin/reset                                   Reset to seed
  POST /api/acme/tasks/preview                        Guarded op; preview (no write)
  POST /api/acme/tasks                                Guarded op; idempotent create
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import render, seed, store

app = FastAPI(title="Northstar demo product", version=seed.DEMO_VERSION)


def _html(s: str) -> HTMLResponse:
    return HTMLResponse(s)


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return _html(render.home())


@app.get("/customers", response_class=HTMLResponse)
def customers() -> HTMLResponse:
    return _html(render.customers())


@app.get("/customers/acme-robotics", response_class=HTMLResponse)
def acme() -> HTMLResponse:
    return _html(render.acme_account())


@app.get("/customers/acme-robotics/onboarding", response_class=HTMLResponse)
def onboarding() -> HTMLResponse:
    return _html(render.onboarding_checklist())


@app.get("/customers/acme-robotics/onboarding/{stage}", response_class=HTMLResponse)
def onboarding_stage(stage: str) -> HTMLResponse:
    return _html(render.onboarding_stage(stage))


@app.get("/customers/acme-robotics/implementation", response_class=HTMLResponse)
def implementation() -> HTMLResponse:
    return _html(render.implementation())


@app.get("/tasks", response_class=HTMLResponse)
def tasks(created: int = 0) -> HTMLResponse:
    flash = None
    if created:
        # Re-derive from the canonical key so a refresh after create shows the
        # confirmation without a second write (the row already exists).
        existing = store.task_by_idempotency_key(seed.FOLLOWUP_IDEMPOTENCY_KEY)
        if existing:
            flash = {"created": False, "task": existing}
    return _html(render.tasks(flash))


@app.get("/security", response_class=HTMLResponse)
def security() -> HTMLResponse:
    return _html(render.security())


# ── health + reset ─────────────────────────────────────────────────────────
@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "demo": seed.COMPANY_ID,
        "version": seed.DEMO_VERSION,
        "customer": seed.CUSTOMER_ID,
        "tasks": len(store.tasks()),
    })


@app.post("/admin/reset")
def admin_reset() -> JSONResponse:
    store.reset()
    return JSONResponse({"reset": True, "tasks": len(store.tasks())})


# ── the guarded follow-up operation ────────────────────────────────────────
async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001; empty/invalid body is fine (use defaults)
        return {}
    return body if isinstance(body, dict) else {}


@app.post("/api/acme/tasks/preview")
async def preview_task(request: Request) -> JSONResponse:
    """PURE READ. Returns the exact task a create would write. Never mutates."""
    b = await _json_body(request)
    return JSONResponse(store.preview_followup_task(
        b.get("idempotency_key"), b.get("title"), b.get("note")))


@app.post("/api/acme/tasks")
async def create_task(request: Request) -> JSONResponse:
    """Idempotent create. First approved call writes the task; later calls with
    the same idempotency key write nothing and report created=false."""
    b = await _json_body(request)
    result = store.create_followup_task(
        b.get("idempotency_key"), b.get("title"), b.get("note"))
    return JSONResponse(result, status_code=201 if result["created"] else 200)
