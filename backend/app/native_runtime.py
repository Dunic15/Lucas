"""Compatibility shim: native_runtime moved to app.runtime.native_runtime."""
import sys as _sys
from .runtime import native_runtime as _mod
_sys.modules[__name__]=_mod
