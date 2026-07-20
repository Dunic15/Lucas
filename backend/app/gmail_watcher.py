"""Compatibility shim: gmail_watcher moved to app.integrations.gmail_watcher."""
import sys as _sys
from .integrations import gmail_watcher as _mod
_sys.modules[__name__] = _mod
