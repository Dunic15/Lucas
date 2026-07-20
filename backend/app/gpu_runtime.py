"""Compatibility shim: gpu_runtime moved to app.runtime.gpu_runtime."""
import sys as _sys
from .runtime import gpu_runtime as _mod
_sys.modules[__name__]=_mod
