"""Compatibility shim: emotion moved to app.meeting.emotion."""
import sys as _sys
from .meeting import emotion as _mod
_sys.modules[__name__]=_mod
