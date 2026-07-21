"""Compatibility shim: config moved to app.core.config."""
import sys as _sys
from .core import config as _mod
_sys.modules[__name__]=_mod
