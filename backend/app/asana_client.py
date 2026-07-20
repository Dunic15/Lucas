"""Compatibility shim: asana_client moved to app.integrations.asana_client."""
import sys as _sys
from .integrations import asana_client as _mod
_sys.modules[__name__] = _mod
