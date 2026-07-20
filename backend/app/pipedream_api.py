"""Compatibility shim: pipedream_api moved to app.api.pipedream_api."""
import sys as _sys
from .api import pipedream_api as _mod
_sys.modules[__name__]=_mod
