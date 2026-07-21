"""Compatibility shim: decision moved to app.meeting.decision."""
import sys as _sys
from .meeting import decision as _mod
_sys.modules[__name__]=_mod
