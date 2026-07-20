"""Compatibility shim: action_reconcile moved to app.actions.action_reconcile."""
import sys as _sys
from .actions import action_reconcile as _mod
_sys.modules[__name__]=_mod
