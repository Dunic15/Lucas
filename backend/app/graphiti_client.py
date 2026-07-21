"""Compatibility shim: graphiti_client moved to app.integrations.graphiti_client."""
import sys as _sys
from .integrations import graphiti_client as _mod
_sys.modules[__name__] = _mod
