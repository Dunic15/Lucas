"""Compatibility shim: jira_client moved to app.integrations.jira_client."""
import sys as _sys
from .integrations import jira_client as _mod
_sys.modules[__name__] = _mod
