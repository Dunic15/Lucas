"""Compatibility shim: drive_client moved to app.integrations.drive_client."""
import sys as _sys
from .integrations import drive_client as _mod
_sys.modules[__name__] = _mod
