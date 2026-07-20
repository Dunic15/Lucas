"""Compatibility shim: entitlements moved to app.core.entitlements."""
import sys as _sys
from .core import entitlements as _mod
_sys.modules[__name__]=_mod
