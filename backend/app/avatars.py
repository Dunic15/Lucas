"""Compatibility shim: avatars moved to app.avatar.avatars."""
import sys as _sys
from .avatar import avatars as _mod
_sys.modules[__name__]=_mod
