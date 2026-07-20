"""Compatibility shim: org_api moved to app.api.org_api."""
import sys as _sys
from .api import org_api as _mod
_sys.modules[__name__]=_mod
