"""Compatibility shim: outbox moved to app.actions.outbox."""
import sys as _sys
from .actions import outbox as _mod
_sys.modules[__name__]=_mod
