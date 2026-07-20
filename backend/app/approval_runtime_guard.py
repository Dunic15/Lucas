"""Compatibility guard for Laura-owned action execution.

Cedric remains the Slack conversation and status surface. Approved writes must
execute inside Laura, so the two legacy approval seams are fenced without
changing Slack OAuth, inbound relay, or status projection.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Callable

from fastapi import FastAPI, Request

from .config import settings

_APPROVAL_REQUEST: ContextVar[bool] = ContextVar(
    "laura_native_approval_request", default=False
)
_PATCHED = False


def _is_approval_path(request: Request) -> bool:
    path = request.url.path.rstrip("/")
    return (
        request.method == "POST"
        and path.endswith("/approve")
        and (
            path.startswith("/dashboard/actions/")
            or path.startswith("/org/actions/")
        )
    )


def _patch_runtime() -> None:
    """Patch legacy execution seams once, preserving Slack presentation paths."""
    global _PATCHED
    if _PATCHED:
        return

    from . import executor, org_api
    from .cedric import callback as cedric_callback

    original_dispatch = cedric_callback.dispatch_action

    def _approval_safe_dispatch(*args, **kwargs):
        if _APPROVAL_REQUEST.get():
            reason = "laura_native_only" if settings.native_executor else "not_configured"
            return {"ok": False, "reason": reason}
        return original_dispatch(*args, **kwargs)

    _approval_safe_dispatch.__name__ = getattr(
        original_dispatch, "__name__", "dispatch_action"
    )
    _approval_safe_dispatch.__doc__ = getattr(original_dispatch, "__doc__", None)
    cedric_callback.dispatch_action = _approval_safe_dispatch

    original_execute_route = org_api._execute_route

    def _laura_execute_route(
        org: str,
        action_id: str,
        action: dict,
        acting_avatar: str = "",
        *,
        idempotency_key: str = "",
        via: str = "",
    ):
        route = str((action or {}).get("execution_route") or "").strip()
        # Historical direct approvals may still carry execution_route=cedric.
        # Supported typed actions are migrated into Laura's native runtime.
        # Dependency-release rows retain their existing compatibility behavior.
        if route != "browser" and via != "dependency-release":
            native = executor.from_typed((action or {}).get("typed"))
            if native is not None and executor.handles(native):
                action = dict(action or {})
                action["execution_route"] = "native"
        return original_execute_route(
            org,
            action_id,
            action,
            acting_avatar,
            idempotency_key=idempotency_key,
            via=via,
        )

    org_api._execute_route = _laura_execute_route

    try:
        from . import dashboard

        dashboard._DISPATCH_FAILURE_MESSAGE["laura_native_only"] = (
            "Laura could not execute this action with a connected native tool. "
            "Nothing ran."
        )
    except Exception:  # pragma: no cover - dashboard exists in production
        pass

    _PATCHED = True


def install(app: FastAPI) -> None:
    """Install request-local approval context and legacy route patches."""
    _patch_runtime()

    @app.middleware("http")
    async def _laura_approval_context(request: Request, call_next: Callable):
        token = _APPROVAL_REQUEST.set(_is_approval_path(request))
        try:
            return await call_next(request)
        finally:
            _APPROVAL_REQUEST.reset(token)
