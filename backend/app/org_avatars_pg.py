"""Compatibility shim: org_avatars_pg moved to app.persistence.org_avatars_pg."""
import sys as _sys
from .persistence import org_avatars_pg as _mod
_sys.modules[__name__]=_mod
