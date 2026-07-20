"""Compatibility shim: gemini_ears moved to app.integrations.gemini_ears."""
import sys as _sys
from .integrations import gemini_ears as _mod
_sys.modules[__name__] = _mod
