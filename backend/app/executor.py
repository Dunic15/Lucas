"""Compatibility shim: executor moved to app.actions.executor."""
import sys as _sys
from .actions import executor as _mod
_sys.modules[__name__]=_mod
