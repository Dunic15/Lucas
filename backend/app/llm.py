"""Compatibility shim: llm moved to app.brain.llm."""
import sys as _sys
from app.brain import llm as _mod
_sys.modules[__name__]=_mod
