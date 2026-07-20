"""Compatibility shim: outbox_pg moved to app.actions.outbox_pg.
Kept so existing ``app.outbox_pg`` imports resolve during the restructure."""
import sys as _sys
from app.actions import outbox_pg as _mod
_sys.modules[__name__] = _mod
