"""Compatibility shim: scheduler moved to app.actions.scheduler."""
import sys as _sys
from .actions import scheduler as _mod
_sys.modules[__name__]=_mod
