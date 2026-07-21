"""Compatibility shim: vendor_health moved to app.integrations.vendor_health."""
import sys as _sys
from .integrations import vendor_health as _mod
_sys.modules[__name__] = _mod
