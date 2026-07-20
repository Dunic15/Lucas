"""Compatibility shim: action_reconcile moved to app.actions.action_reconcile.
Kept so existing ``app.action_reconcile`` imports resolve during the restructure."""
import sys as _sys
from app.actions import action_reconcile as _mod
_sys.modules[__name__] = _mod
