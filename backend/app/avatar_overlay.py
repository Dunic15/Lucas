"""Compatibility shim: avatar_overlay moved to app.avatar.avatar_overlay."""
import sys as _sys
from .avatar import avatar_overlay as _mod
_sys.modules[__name__]=_mod
