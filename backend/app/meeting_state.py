"""Compatibility shim: meeting_state moved to app.meeting.meeting_state."""
import sys as _sys
from app.meeting import meeting_state as _mod
_sys.modules[__name__]=_mod
