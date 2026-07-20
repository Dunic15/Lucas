"""Compatibility shim: runpod_runtime moved to app.runtime.runpod_runtime."""
import sys as _sys
from .runtime import runpod_runtime as _mod
_sys.modules[__name__]=_mod
