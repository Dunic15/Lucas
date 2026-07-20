"""Compatibility shim: ledger moved to app.actions.ledger.
Kept so existing ``app.ledger`` imports resolve during the restructure."""
import sys as _sys
from app.actions import ledger as _mod
_sys.modules[__name__] = _mod
