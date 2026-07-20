"""Compatibility shim: llm moved to app.brain.llm."""
import sys as _sys
from .brain import llm as _mod
_sys.modules[__name__]=_mod
