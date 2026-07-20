"""Compatibility shim: crypto moved to app.core.crypto."""
import sys as _sys
from app.core import crypto as _mod
_sys.modules[__name__]=_mod
