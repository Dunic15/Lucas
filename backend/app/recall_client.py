"""Compatibility shim: recall_client moved to app.integrations.recall_client.
Kept so existing ``app.recall_client`` imports keep resolving during the restructure;
remove once call-sites import from app.integrations directly."""
import sys as _sys
from app.integrations import recall_client as _mod
_sys.modules[__name__] = _mod
