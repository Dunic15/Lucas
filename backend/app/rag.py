"""Compatibility shim: rag moved to app.brain.rag."""
import sys as _sys
from app.brain import rag as _mod
_sys.modules[__name__]=_mod
