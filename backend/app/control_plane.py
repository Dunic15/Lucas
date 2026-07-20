"""Compatibility shim: control_plane moved to app.persistence.control_plane."""
import sys as _sys
from app.persistence import control_plane as _mod
_sys.modules[__name__]=_mod
