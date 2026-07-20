"""Compatibility shim: store moved to app.persistence.store."""
import sys as _sys
from .persistence import store as _mod
_sys.modules[__name__]=_mod
