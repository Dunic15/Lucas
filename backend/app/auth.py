"""Compatibility shim: auth moved to app.core.auth."""
import sys as _sys
from app.core import auth as _mod
_sys.modules[__name__]=_mod
