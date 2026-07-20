"""Compatibility shim: scheduler moved to app.actions.scheduler.
Kept so existing ``app.scheduler`` imports resolve during the restructure."""
import sys as _sys
from app.actions import scheduler as _mod
_sys.modules[__name__] = _mod
