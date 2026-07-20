"""Compatibility shim: end_of_turn moved to app.meeting.end_of_turn."""
import sys as _sys
from app.meeting import end_of_turn as _mod
_sys.modules[__name__]=_mod
