"""Compatibility shim: granola_client moved to app.integrations.granola_client.
Kept so existing ``app.granola_client`` imports keep resolving during the restructure;
remove once call-sites import from app.integrations directly."""
import sys as _sys
from app.integrations import granola_client as _mod
_sys.modules[__name__] = _mod
