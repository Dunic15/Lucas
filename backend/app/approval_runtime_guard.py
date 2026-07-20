"""Compatibility shim: approval_runtime_guard moved to app.actions.approval_runtime_guard."""
import sys as _sys
from .actions import approval_runtime_guard as _mod
_sys.modules[__name__]=_mod
