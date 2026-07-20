"""Compatibility shim: tts moved to app.integrations.tts."""
import sys as _sys
from app.integrations import tts as _mod
_sys.modules[__name__]=_mod
