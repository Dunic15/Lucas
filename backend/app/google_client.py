"""Compatibility shim: google_client moved to app.integrations.google_client.
Kept so existing ``app.google_client`` imports keep resolving during the restructure;
remove once call-sites import from app.integrations directly."""
import sys as _sys
from app.integrations import google_client as _mod
_sys.modules[__name__] = _mod
