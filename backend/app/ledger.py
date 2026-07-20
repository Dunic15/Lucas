"""Compatibility shim: ledger moved to app.actions.ledger."""
import sys as _sys
from .actions import ledger as _mod
_sys.modules[__name__]=_mod
