"""Compatibility shim: outbox_pg moved to app.actions.outbox_pg."""
import sys as _sys
from .actions import outbox_pg as _mod
_sys.modules[__name__]=_mod
