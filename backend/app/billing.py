"""Compatibility shim: billing moved to app.persistence.billing."""
import sys as _sys
from app.persistence import billing as _mod
_sys.modules[__name__]=_mod
