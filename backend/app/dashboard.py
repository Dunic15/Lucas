"""Compatibility shim: dashboard moved to app.api.dashboard."""
import sys as _sys
from .api import dashboard as _mod
_sys.modules[__name__]=_mod
