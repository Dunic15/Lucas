"""Compatibility shim: outbox moved to app.actions.outbox.
Kept so existing ``app.outbox`` imports resolve during the restructure."""
import sys as _sys
from app.actions import outbox as _mod
_sys.modules[__name__] = _mod
