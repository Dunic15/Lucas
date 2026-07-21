"""Compatibility shim: google_client moved to app.integrations.google_client."""
import sys as _sys
from .integrations import google_client as _mod
_sys.modules[__name__] = _mod
