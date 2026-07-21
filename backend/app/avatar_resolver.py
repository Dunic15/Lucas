"""Compatibility shim: avatar_resolver moved to app.avatar.avatar_resolver."""
import sys as _sys
from .avatar import avatar_resolver as _mod
_sys.modules[__name__]=_mod
