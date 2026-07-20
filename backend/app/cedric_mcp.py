"""Compatibility shim: cedric_mcp moved to app.cedric.cedric_mcp."""
import sys as _sys
from app.cedric import cedric_mcp as _mod
_sys.modules[__name__]=_mod
