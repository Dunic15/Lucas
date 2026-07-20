"""Compatibility shim: autopilot moved to app.actions.autopilot.
Kept so existing ``app.autopilot`` imports resolve during the restructure."""
import sys as _sys
from app.actions import autopilot as _mod
_sys.modules[__name__] = _mod
