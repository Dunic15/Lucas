"""Compatibility shim: action_plane moved to app.actions.action_plane.
Kept so existing ``app.action_plane`` imports resolve during the restructure."""
import sys as _sys
from app.actions import action_plane as _mod
_sys.modules[__name__] = _mod
