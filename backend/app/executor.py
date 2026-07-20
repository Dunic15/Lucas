"""Compatibility shim: executor moved to app.actions.executor.
Kept so existing ``app.executor`` imports resolve during the restructure."""
import sys as _sys
from app.actions import executor as _mod
_sys.modules[__name__] = _mod
