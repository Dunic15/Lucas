"""Compatibility shim: security moved to app.core.security."""
import sys as _sys
from app.core import security as _mod
_sys.modules[__name__]=_mod
