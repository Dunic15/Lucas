"""Compatibility shim: tts moved to app.integrations.tts."""
import sys as _sys
from .integrations import tts as _mod
_sys.modules[__name__] = _mod
