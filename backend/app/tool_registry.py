"""Compatibility shim: tool_registry moved to app.brain.tool_registry."""
import sys as _sys
from .brain import tool_registry as _mod
_sys.modules[__name__]=_mod
