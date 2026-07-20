"""Compatibility shim: drive_client moved to app.integrations.drive_client.
Kept so existing ``app.drive_client`` imports keep resolving during the restructure;
remove once call-sites import from app.integrations directly."""
import sys as _sys
from app.integrations import drive_client as _mod
_sys.modules[__name__] = _mod
