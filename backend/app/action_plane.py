"""Compatibility shim: action_plane moved to app.actions.action_plane."""
import sys as _sys
from .actions import action_plane as _mod
_sys.modules[__name__]=_mod
