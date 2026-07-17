"""Avatar Studio HTTP surface (M2) — /org/avatars/* + the dashboard twin.

Reads are for org members; every WRITE is admin-gated: personal-org owners
(user_id == org_id), durable members with role owner|admin
(laura_private.billing_member_role via control_plane.member_role — called
with the durable member_uid, never the u_<hash> session id), or the org's
own machine bearer (the org credential IS the org's authority). The
dashboard twin requires cookie + same-origin on every mutation and checks
the METHOD on every branch (the knowledge twin's leniency is not copied).

Validation order for writes: typed allowlist (avatar_overlay.validate_overlay)
→ server-side voice whitelist → context-scope org-membership check against
the org's OWN Company Brain sources. Published versions are immutable;
publish/rollback invalidate the resolver cache locally (other instances
converge within its 60s TTL) and enqueue a knowledge index rebuild when a
context scope is present (so per-chunk source ids exist to filter on).

No secrets in any response; overlay text is bounded at validation time and
escaped client-side. Nothing here touches the live transcript path.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import (
    auth, avatar_overlay, avatar_resolver, avatars, control_plane,
    org_avatars_pg,
)
from .config import settings

router = APIRouter(tags=["org-avatars"])

_NO_STORE = {"Cache-Control": "no-store"}
_VOICE_ID = re.compile(r"^[A-Za-z0-9_-]{12,40}$")


def _disabled() -> JSONResponse:
    return JSONResponse(
        {"error": "org_avatar_overlays_disabled"}, status_code=404,
        headers=_NO_STORE,
    )


# ── auth gates ──────────────────────────────────────────────────────────────

async def _machine_gate(request: Request):
    from . import org_api

    return await org_api._machine_gate(request)


def _dash_user(request: Request):
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err, None
        return JSONResponse({"error": "login required"}, status_code=401), None
    return None, user


def _admin_error(user: dict) -> str:
    """'' when this logged-in user may WRITE overlays for their org."""
    org = str(user.get("org_id") or "")
    if not org:
        return "admin_required"
    if str(user.get("user_id") or "") == org:
        return ""  # personal org: the owner is the org
    member_uid = str(user.get("member_uid") or "")
    if member_uid:
        try:
            role = control_plane.member_role(org, member_uid)
        except Exception:  # noqa: BLE001 — role read failure fails CLOSED
            role = None
        if role in ("owner", "admin"):
            return ""
    return "admin_required"


# ── enumerable choices (server-validated; never free-form provider ids) ─────

_voice_cache: list[dict] | None = None


def _voice_whitelist() -> list[dict]:
    """Curated ElevenLabs STOCK voices: voice-previews/ids.json plus every id
    already used in avatar.yaml/config. Free-form ids are refused — a dead or
    plan-gated voice id would poison the live-path fallback memo for everyone
    on the shared key."""
    global _voice_cache
    if _voice_cache is not None:
        return _voice_cache
    found: dict[str, str] = {}

    def _walk(node, label=""):
        if isinstance(node, dict):
            candidate = str(node.get("id") or node.get("voice_id") or "")
            name = str(node.get("name") or node.get("label") or label)
            if _VOICE_ID.match(candidate):
                found.setdefault(candidate, name or candidate)
            for key, value in node.items():
                _walk(value, label=str(key))
        elif isinstance(node, list):
            for item in node:
                _walk(item, label=label)
        elif isinstance(node, str) and _VOICE_ID.match(node):
            found.setdefault(node, label or node)

    ids_file = Path(settings.avatars_dir).parent / "voice-previews" / "ids.json"
    try:
        _walk(json.loads(ids_file.read_text()))
    except (OSError, ValueError):
        pass
    for avatar_id in avatars.list_ids():
        try:
            voice = avatars.load(avatar_id).elevenlabs_voice_id
        except Exception:  # noqa: BLE001
            continue
        if voice and _VOICE_ID.match(voice):
            found.setdefault(voice, f"{avatar_id} (current)")
    fallback = settings.elevenlabs_fallback_voice_id
    if fallback and _VOICE_ID.match(fallback):
        found.setdefault(fallback, "stock fallback")
    _voice_cache = [
        {"id": vid, "label": label} for vid, label in sorted(found.items())
    ]
    return _voice_cache


def _family_connected(org: str) -> dict[str, bool]:
    """Per-family connected-account availability — the SAME heterogeneous
    signals dashboard_summary derives (org_oauth google, cedric-brain row for
    slack, asana PAT/env), factored for the Studio preview."""
    from . import asana_client, store

    google = False
    try:
        google = bool(store.get_org_oauth(org))
    except Exception:  # noqa: BLE001
        pass
    slack = False
    try:
        from . import dashboard

        slack = dashboard._org_connected(
            dashboard._org_connection_rows(org), "cedric-brain"
        )
    except Exception:  # noqa: BLE001
        pass
    asana = False
    try:
        asana = bool(asana_client.connected(org))
    except Exception:  # noqa: BLE001
        pass
    return {"google": google, "slack": slack, "asana": asana}


def _brain_sources(org: str) -> list[dict]:
    from . import knowledge
    from .knowledge import dal as knowledge_dal

    if not knowledge.enabled():
        return []
    try:
        return [
            {"id": s["id"], "name": s["name"], "kind": s["kind"],
             "documents": int(s.get("published_documents") or 0)}
            for s in knowledge_dal.list_sources(org)
        ]
    except Exception:  # noqa: BLE001 — the Studio shows an empty list
        return []


# ── validation beyond the pure allowlist ────────────────────────────────────

def _server_checks(org: str, overlay: dict) -> list[str]:
    errors: list[str] = []
    voice = overlay.get("voice_id")
    if voice and voice not in {v["id"] for v in _voice_whitelist()}:
        errors.append("voice_id is not in the approved voice list")
    scope = overlay.get("context_scope")
    if isinstance(scope, dict) and scope.get("knowledge_source_ids"):
        from . import knowledge

        if not knowledge.enabled():
            errors.append(
                "context_scope references Company Brain sources but the "
                "Company Brain is not enabled"
            )
        else:
            own = {s["id"] for s in _brain_sources(org)}
            foreign = [
                sid for sid in scope["knowledge_source_ids"] if sid not in own
            ]
            if foreign:
                errors.append(
                    "context_scope sources not in this organization: "
                    + ", ".join(foreign[:5])
                )
    return errors


def _validate(org: str, avatar_key: str, payload) -> tuple[dict, list[str]]:
    try:
        canonical = avatars.load(avatar_key)
    except FileNotFoundError:
        return {}, ["unknown avatar"]
    clean, errors = avatar_overlay.validate_overlay(canonical, payload)
    if not errors:
        errors = _server_checks(org, clean)
    return clean, errors


def _resolved_summary(org: str, avatar_key: str,
                      overlay: dict | None = None) -> dict:
    """The preview payload: what THIS overlay (or the published one) resolves
    to — greeting, prompt addition, effective tools, scope, warnings."""
    canonical = avatars.load(avatar_key)
    if overlay is None:
        resolved = avatar_resolver.describe(org, avatar_key)
        applied = resolved.overlay_version > 0
    else:
        resolved = avatar_resolver._apply(
            canonical, org, {"version": -1, "overlay": overlay}
        )
        applied = True
    connected = _family_connected(org)
    warnings: list[str] = []
    for family in resolved.effective_tools:
        if family in connected and not connected[family]:
            warnings.append(
                f"'{family}' is enabled but the account is not connected"
            )
    if resolved.face == "photoreal" and not (
        canonical.renderer_readiness.get("photoreal", {}).get("ready")
    ):
        warnings.append("photoreal face selected but its assets are missing")
    scope = resolved.context_scope
    prov = resolved.resolver_provenance
    return {
        "avatar_key": avatar_key,
        "applied": applied,
        "canonical_name": canonical.name,
        "name": resolved.name,
        "role": resolved.role,
        "greeting": prov.get("greeting", ""),
        "mission": resolved.mission,
        "voice_id": resolved.elevenlabs_voice_id,
        "face": resolved.page,
        "talk_body": resolved.talk_body,
        "wake_words": resolved.wake_words,
        "prompt_addition": avatar_overlay.preferences_block(
            overlay if overlay is not None else
            _current_overlay_payload(org, avatar_key)
        ),
        "effective_tools": sorted(resolved.effective_tools),
        "capability_ceiling": sorted(
            avatar_overlay.capability_ceiling(canonical)
        ),
        "connected": connected,
        "context_scope": scope,
        "overlay_version": resolved.overlay_version,
        "warnings": warnings,
    }


def _current_overlay_payload(org: str, avatar_key: str) -> dict:
    row = org_avatars_pg.current_overlay(org, avatar_key)
    return (row or {}).get("overlay") or {}


def _after_publish(org: str, avatar_key: str) -> None:
    """Post-publish convergence: resolver cache + knowledge index rebuild
    (per-chunk source ids must exist before a context scope can filter)."""
    avatar_resolver.invalidate(org, avatar_key)
    overlay = _current_overlay_payload(org, avatar_key)
    scope = overlay.get("context_scope") or {}
    if scope.get("knowledge_source_ids"):
        try:
            from . import knowledge
            from .knowledge import dal as knowledge_dal

            if knowledge.enabled():
                sources = knowledge_dal.list_sources(org)
                if sources:
                    knowledge_dal.enqueue_job(
                        org, sources[0]["id"], "rebuild_index"
                    )
        except Exception:  # noqa: BLE001 — the 60s epoch sweep is the backstop
            pass


# ── the shared sync operations (both doors call these) ──────────────────────

def _op_list(org: str) -> tuple[int, dict]:
    rows = {r["avatar_key"]: r for r in org_avatars_pg.list_org_avatars(org)}
    out = []
    for key in avatars.list_for_org(org):
        row = rows.get(key)
        try:
            canonical = avatars.load(key)
        except Exception:  # noqa: BLE001
            continue
        out.append({
            "avatar_key": key,
            "canonical_name": canonical.name,
            "enabled": bool(row["enabled"]) if row else True,
            "published_version": int(row["current_version"]) if row else 0,
            "has_draft": bool(row and row["has_draft"]),
            "assignments": int(row["assignments"]) if row else 0,
        })
    return 200, {"avatars": out}


def _op_get(org: str, key: str) -> tuple[int, dict]:
    if key not in avatars.list_for_org(org):
        return 404, {"error": "unknown avatar"}
    draft = org_avatars_pg.get_draft(org, key)
    return 200, {
        "resolved": _resolved_summary(org, key),
        "draft": draft,
        "versions": org_avatars_pg.list_versions(org, key),
        "published_overlay": _current_overlay_payload(org, key),
    }


def _op_save_draft(org: str, key: str, body: dict, actor: str) -> tuple[int, dict]:
    if key not in avatars.list_for_org(org):
        return 404, {"error": "unknown avatar"}
    clean, errors = _validate(org, key, (body or {}).get("overlay"))
    if errors:
        return 422, {"error": "invalid_overlay", "details": errors[:10]}
    org_avatars_pg.ensure_org_avatar(org, key, actor)
    draft, err = org_avatars_pg.save_draft(
        org, key, clean, actor,
        expected_token=str((body or {}).get("expected_token") or ""),
        change_note=str((body or {}).get("change_note") or ""),
    )
    if err:
        return 409, {"error": err}
    return 200, {"ok": True, "draft": draft}


def _op_publish(org: str, key: str, body: dict, actor: str) -> tuple[int, dict]:
    draft = org_avatars_pg.get_draft(org, key)
    if draft is None:
        return 409, {"error": "no_draft"}
    # Re-validate the exact payload being frozen (sources may have been
    # deleted since the draft was saved).
    _clean, errors = _validate(org, key, draft["overlay_json"])
    if errors:
        return 422, {"error": "invalid_overlay", "details": errors[:10]}
    version, err = org_avatars_pg.publish_draft(
        org, key, actor,
        expected_token=str((body or {}).get("expected_token") or ""),
    )
    if err:
        return 409, {"error": err}
    _after_publish(org, key)
    return 200, {"ok": True, "published_version": version}


def _op_rollback(org: str, key: str, body: dict, actor: str) -> tuple[int, dict]:
    try:
        version = int((body or {}).get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version <= 0:
        return 400, {"error": "version is required"}
    new_version, err = org_avatars_pg.publish_prior(org, key, version, actor)
    if err:
        return 409 if err != "unknown_version" else 404, {"error": err}
    _after_publish(org, key)
    return 200, {"ok": True, "published_version": new_version}


def _op_enable(org: str, key: str, body: dict, actor: str) -> tuple[int, dict]:
    enabled_ = bool((body or {}).get("enabled", True))
    org_avatars_pg.ensure_org_avatar(org, key, actor)
    org_avatars_pg.set_enabled(org, key, enabled_, actor)
    avatar_resolver.invalidate(org, key)
    return 200, {"ok": True, "enabled": enabled_}


def _op_preview(org: str, key: str, body: dict) -> tuple[int, dict]:
    payload = (body or {}).get("overlay")
    if payload is None:
        draft = org_avatars_pg.get_draft(org, key)
        payload = draft["overlay_json"] if draft else None
    if payload is not None:
        clean, errors = _validate(org, key, payload)
        if errors:
            return 422, {"error": "invalid_overlay", "details": errors[:10]}
        return 200, {"preview": _resolved_summary(org, key, clean)}
    return 200, {"preview": _resolved_summary(org, key)}


def _op_capabilities(org: str, key: str) -> tuple[int, dict]:
    try:
        canonical = avatars.load(key)
    except FileNotFoundError:
        return 404, {"error": "unknown avatar"}
    return 200, {
        "capability_ceiling": sorted(
            avatar_overlay.capability_ceiling(canonical)
        ),
        "connected": _family_connected(org),
        "voices": _voice_whitelist(),
        "faces": ["talk", "photoreal"],
        "bodies": ["F", "M"],
        "brain_sources": _brain_sources(org),
        "allowed_fields": sorted(avatar_overlay.ALLOWED_FIELDS),
    }


def _op_assignments(org: str, method: str, body: dict, actor: str,
                    assignment_id: str = "") -> tuple[int, dict]:
    if method == "GET":
        return 200, {"assignments": org_avatars_pg.list_assignments(org)}
    if method == "POST":
        row, err = org_avatars_pg.upsert_assignment(
            org,
            str((body or {}).get("avatar_key") or ""),
            str((body or {}).get("scope_kind") or ""),
            str((body or {}).get("scope_value") or ""),
            actor,
        )
        if err:
            return 400 if err != "unknown_avatar" else 404, {"error": err}
        return 200, {"ok": True, "assignment": row}
    if method == "DELETE":
        ok = org_avatars_pg.delete_assignment(org, assignment_id, actor)
        return (200, {"ok": True}) if ok else (404, {"error": "unknown assignment"})
    return 405, {"error": "method not allowed"}


# ── machine surface ─────────────────────────────────────────────────────────

async def _machine_route(request: Request, handler, *args) -> JSONResponse:
    if not org_avatars_pg.enabled():
        return _disabled()
    err, org = await _machine_gate(request)
    if err:
        return err
    code, payload = await run_in_threadpool(handler, org, *args)
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.get("/org/avatars")
async def org_avatars_list(request: Request) -> JSONResponse:
    return await _machine_route(request, _op_list)


@router.get("/org/avatars/assignments")
async def org_assignments_get(request: Request) -> JSONResponse:
    return await _machine_route(
        request, lambda org: _op_assignments(org, "GET", {}, "machine")
    )


@router.post("/org/avatars/assignments")
async def org_assignments_post(request: Request) -> JSONResponse:
    if not org_avatars_pg.enabled():
        return _disabled()
    err, org = await _machine_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    code, payload = await run_in_threadpool(
        _op_assignments, org, "POST", body, "machine"
    )
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.delete("/org/avatars/assignments/{assignment_id}")
async def org_assignments_delete(
    assignment_id: str, request: Request
) -> JSONResponse:
    return await _machine_route(
        request,
        lambda org: _op_assignments(org, "DELETE", {}, "machine",
                                    assignment_id),
    )


@router.get("/org/avatars/{avatar_key}")
async def org_avatar_get(avatar_key: str, request: Request) -> JSONResponse:
    return await _machine_route(request, _op_get, avatar_key)


@router.put("/org/avatars/{avatar_key}/draft")
async def org_avatar_draft(avatar_key: str, request: Request) -> JSONResponse:
    if not org_avatars_pg.enabled():
        return _disabled()
    err, org = await _machine_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    code, payload = await run_in_threadpool(
        _op_save_draft, org, avatar_key, body, "machine"
    )
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.post("/org/avatars/{avatar_key}/publish")
async def org_avatar_publish(avatar_key: str, request: Request) -> JSONResponse:
    if not org_avatars_pg.enabled():
        return _disabled()
    err, org = await _machine_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    code, payload = await run_in_threadpool(
        _op_publish, org, avatar_key, body, "machine"
    )
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.post("/org/avatars/{avatar_key}/rollback")
async def org_avatar_rollback(avatar_key: str, request: Request) -> JSONResponse:
    if not org_avatars_pg.enabled():
        return _disabled()
    err, org = await _machine_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    code, payload = await run_in_threadpool(
        _op_rollback, org, avatar_key, body, "machine"
    )
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.get("/org/avatars/{avatar_key}/versions")
async def org_avatar_versions(avatar_key: str, request: Request) -> JSONResponse:
    return await _machine_route(
        request,
        lambda org: (200, {"versions": org_avatars_pg.list_versions(org, avatar_key)}),
    )


@router.post("/org/avatars/{avatar_key}/preview")
async def org_avatar_preview(avatar_key: str, request: Request) -> JSONResponse:
    if not org_avatars_pg.enabled():
        return _disabled()
    err, org = await _machine_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    code, payload = await run_in_threadpool(_op_preview, org, avatar_key, body)
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.get("/org/avatars/{avatar_key}/capabilities")
async def org_avatar_capabilities(
    avatar_key: str, request: Request
) -> JSONResponse:
    return await _machine_route(request, _op_capabilities, avatar_key)


# ── dashboard twin (cookie + same-origin; strict method checks) ─────────────

@router.api_route(
    "/dashboard/avatar-studio/{tail:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
)
async def dashboard_avatar_studio(tail: str, request: Request) -> JSONResponse:
    if not org_avatars_pg.enabled():
        return _disabled()
    err, user = _dash_user(request)
    if err:
        return err
    org = str(user.get("org_id") or "")
    method = request.method
    if method != "GET":
        if not auth._same_origin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        admin_err = _admin_error(user)
        if admin_err:
            return JSONResponse({"error": admin_err}, status_code=403)
    actor = str(user.get("user_id") or "")
    parts = [p for p in (tail or "").split("/") if p]
    body: dict = {}
    if method in ("POST", "PUT"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    def run() -> tuple[int, dict]:
        if parts == ["avatars"] and method == "GET":
            return _op_list(org)
        if parts == ["assignments"]:
            if method in ("GET", "POST"):
                return _op_assignments(org, method, body, actor)
            return 405, {"error": "method not allowed"}
        if len(parts) == 2 and parts[0] == "assignments" and method == "DELETE":
            return _op_assignments(org, "DELETE", {}, actor, parts[1])
        if len(parts) == 2 and parts[0] == "avatars" and method == "GET":
            return _op_get(org, parts[1])
        if len(parts) == 3 and parts[0] == "avatars":
            key, action = parts[1], parts[2]
            if action == "draft" and method == "PUT":
                return _op_save_draft(org, key, body, actor)
            if action == "publish" and method == "POST":
                return _op_publish(org, key, body, actor)
            if action == "rollback" and method == "POST":
                return _op_rollback(org, key, body, actor)
            if action == "enable" and method == "POST":
                return _op_enable(org, key, body, actor)
            if action == "preview" and method == "POST":
                return _op_preview(org, key, body)
            if action == "versions" and method == "GET":
                return 200, {"versions": org_avatars_pg.list_versions(org, key)}
            if action == "capabilities" and method == "GET":
                return _op_capabilities(org, key)
            if action == "audit" and method == "GET":
                return 200, {"audit": org_avatars_pg.list_audit(org, key)}
        return 404, {"error": "unknown avatar-studio route"}

    code, payload = await run_in_threadpool(run)
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)
