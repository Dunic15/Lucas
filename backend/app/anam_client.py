"""Compatibility shim: anam_client moved to app.integrations.anam_client.
Kept so existing ``app.anam_client`` imports keep resolving during the restructure;
remove once call-sites import from app.integrations directly."""
import sys as _sys
from app.integrations import anam_client as _mod
_sys.modules[__name__] = _mod
