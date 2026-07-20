"""Compatibility shim: org_avatars_api moved to app.api.org_avatars_api."""
import sys as _sys
from .api import org_avatars_api as _mod
_sys.modules[__name__]=_mod
