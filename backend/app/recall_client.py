"""Compatibility shim: recall_client moved to app.integrations.recall_client."""
import sys as _sys
from .integrations import recall_client as _mod
_sys.modules[__name__] = _mod
