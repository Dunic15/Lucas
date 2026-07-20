"""Compatibility guard for Laura-owned approval execution.

The product still uses Cedric as the Slack conversation and status surface, but
approved writes must execute inside Laura.  Two older approval handlers retain a
Cedric dispatch branch for backwards compatibility.  This module fences that
branch at request time without changing the working Slack OAuth, inbound relay,
or status-projection paths.

It also normalizes historical ``execution_route=cedric`` rows to Laura native
execution when their typed action now has a registered adapter.  Browser actions
remain on their separately hardened operator route.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Callable

from fastapi import FastAPI, Request

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
    """Patch the two legacy seams once, preserving every non-approval Cedric use."""
    global _PATCHED
    if _PATCHED:
        return

    from . import executor, org_api
    from .cedric import callback as cedric_callback

    original_dispatch = cedric_callback.dispatch_action

    def _approval_safe_dispatch(*args, **kwargs):
        # Outside Laura's canonical approval requests this is byte-identical to
        # the existing callback client.  Inside them Cedric remains a surface,
        # never a fallback executor.
        if _APPROVAL_REQUEST.get():
            return {"ok": False, "reason": "laura_native_only"}
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
        if route != "browser":
            native = executor.from_typed((action or {}).get("typed"))
            if native is not None and executor.handles(native):
                # Historical rows may still say cedric even though Laura now has
                # the adapter.  Feed a copy through the existing native route so
                # capability checks, exactly-once claims and receipts stay
                # canonical and no persisted artifact is silently rewritten.
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

    # The dashboard already turns a refused fallback into a truthful failed
    # receipt.  Give the new reason accurate product wording rather than the
    # generic legacy Cedric error.
    try:
        from . import dashboard

        dashboard._DISPATCH_FAILURE_MESSAGE["laura_native_only"] = (
            "Laura could not execute this action with a connected native tool. "
            "Nothing ran."
        )
    except Exception:  # pragma: no cover - dashboard is present in the app
        pass

    _PATCHED = True


def install(app: FastAPI) -> None:
    """Install the approval context and patch the legacy routing seams."""
    _patch_runtime()

    @app.middleware("http")
    async def _laura_native_approval_context(
        request: Request, call_next: Callable
    ):
        if not _is_approval_path(request):
            return await call_next(request)
        token = _APPROVAL_REQUEST.set(True)
        try:
            return await call_next(request)
        finally:
            _APPROVAL_REQUEST.reset(token)
