"""Compatibility shim: anam_client moved to app.integrations.anam_client."""
import sys as _sys
from .integrations import anam_client as _mod
_sys.modules[__name__] = _mod
