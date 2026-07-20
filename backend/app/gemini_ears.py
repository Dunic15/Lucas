"""Compatibility shim: gemini_ears moved to app.integrations.gemini_ears.
Kept so existing ``app.gemini_ears`` imports keep resolving during the restructure;
remove once call-sites import from app.integrations directly."""
import sys as _sys
from app.integrations import gemini_ears as _mod
_sys.modules[__name__] = _mod
