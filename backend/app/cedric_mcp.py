"""Compatibility shim: cedric_mcp moved to app.cedric.mcp."""
import sys as _sys
from .cedric import mcp as _mod
_sys.modules[__name__]=_mod
