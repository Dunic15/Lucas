"""Compatibility shim: action_deps moved to app.actions.action_deps."""
import sys as _sys
from .actions import action_deps as _mod
_sys.modules[__name__]=_mod
