"""Compatibility shim: autopilot moved to app.actions.autopilot."""
import sys as _sys
from .actions import autopilot as _mod
_sys.modules[__name__]=_mod
