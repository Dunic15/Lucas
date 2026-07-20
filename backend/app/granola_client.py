"""Compatibility shim: granola_client moved to app.integrations.granola_client."""
import sys as _sys
from .integrations import granola_client as _mod
_sys.modules[__name__] = _mod
