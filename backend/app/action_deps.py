"""Compatibility shim: action_deps moved to app.actions.action_deps.
Kept so existing ``app.action_deps`` imports resolve during the restructure."""
import sys as _sys
from app.actions import action_deps as _mod
_sys.modules[__name__] = _mod
