"""Compatibility shim: dashboard_runtime_ui moved to app.api.dashboard_runtime_ui."""
import sys as _sys
from .api import dashboard_runtime_ui as _mod
_sys.modules[__name__]=_mod
