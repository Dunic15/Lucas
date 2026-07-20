"""Compatibility shim: tools moved to app.brain.tools."""
import sys as _sys
from app.brain import tools as _mod
_sys.modules[__name__]=_mod
