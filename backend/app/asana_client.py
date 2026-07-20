"""Compatibility shim: asana_client moved to app.integrations.asana_client.
Kept so existing ``app.asana_client`` imports keep resolving during the restructure;
remove once call-sites import from app.integrations directly."""
import sys as _sys
from app.integrations import asana_client as _mod
_sys.modules[__name__] = _mod
