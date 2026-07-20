"""Compatibility guards for Laura-owned execution and meeting etiquette.

Cedric remains the Slack conversation/status surface, but approved writes execute
inside Laura. The guards below fence the two legacy Cedric execution seams while
leaving Slack OAuth, inbound requests, and status projection unchanged.

The module also preserves the shipped wake-word contract: a partial transcript
may establish that the avatar was addressed, but it must not trigger the audible
acknowledgement while the human is still speaking.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Callable

from fastapi import FastAPI, Request

from .config import settings

_APPROVAL_REQUEST: ContextVar[bool] = ContextVar(
    "laura_native_approval_request", default=False
)
_PARTIAL_TRANSCRIPT_REQUEST: ContextVar[bool] = ContextVar(
    "laura_partial_transcript_request", default=False
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
    """Patch legacy seams once, preserving all non-execution Cedric behavior."""
    global _PATCHED
    if _PATCHED:
        return

    from . import executor, org_api
    from .cedric import callback as cedric_callback

    original_dispatch = cedric_callback.dispatch_action

    def _approval_safe_dispatch(*args, **kwargs):
        if _APPROVAL_REQUEST.get():
            # Preserve the historical flag-off response for compatibility, but
            # do not call Cedric. With Laura's runtime enabled, unsupported work
            # reports the explicit native-only reason instead.
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
        # Direct dashboard/Slack approvals migrate historical typed Cedric rows
        # to Laura. The established dependency-release compatibility contract is
        # intentionally left unchanged: such legacy rows unpark but wait rather
        # than executing through a route their original approval did not claim.
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

    # main.py has imported detect_wake before security.install(app) invokes this
    # module, even though the rest of main is still being defined. The exact
    # fuzzy=False check is used only for the partial-transcript audible ack. Keep
    # the first/default wake detection intact so "Petra stop" still interrupts.
    try:
        from . import main as main_module

        original_detect_wake = main_module.detect_wake

        def _partial_safe_detect_wake(
            avatar, text, present_names=None, *, fuzzy=True
        ):
            result = original_detect_wake(
                avatar, text, present_names, fuzzy=fuzzy
            )
            if (
                _PARTIAL_TRANSCRIPT_REQUEST.get()
                and fuzzy is False
                and result[0]
                and main_module._wake_required(avatar)
            ):
                return False, result[1]
            return result

        main_module.detect_wake = _partial_safe_detect_wake
    except Exception:  # pragma: no cover - app startup provides this seam
        pass

    try:
        from . import dashboard

        dashboard._DISPATCH_FAILURE_MESSAGE["laura_native_only"] = (
            "Laura could not execute this action with a connected native tool. "
            "Nothing ran."
        )
    except Exception:  # pragma: no cover - dashboard is present in the app
        pass

    _PATCHED = True


async def _request_flags(request: Request) -> tuple[bool, bool]:
    """Return (approval, partial_transcript) and replay any consumed body."""
    approval = _is_approval_path(request)
    partial = False
    if request.method == "POST" and request.url.path.rstrip("/") == "/webhooks/recall":
        body = await request.body()
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
            event = str((payload or {}).get("event") or "")
            partial = event in {"transcript.partial_data", "transcript.partial"}
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            partial = False

        sent = False

        async def _receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = _receive  # Starlette body replay for the route below
    return approval, partial


def install(app: FastAPI) -> None:
    """Install request-local execution and partial-transcript guards."""
    _patch_runtime()

    @app.middleware("http")
    async def _laura_runtime_context(request: Request, call_next: Callable):
        approval, partial = await _request_flags(request)
        approval_token = _APPROVAL_REQUEST.set(approval)
        partial_token = _PARTIAL_TRANSCRIPT_REQUEST.set(partial)
        try:
            return await call_next(request)
        finally:
            _PARTIAL_TRANSCRIPT_REQUEST.reset(partial_token)
            _APPROVAL_REQUEST.reset(approval_token)
