"""Laura backend package initialization."""

# A recent main merge retained the optional Graphiti client while dropping its
# Settings fields. Install property-backed, environment-aware compatibility
# fields before any app submodule imports ``settings``. This is inert once the
# canonical fields return to config.py.
from . import graphiti_settings_compat as _graphiti_settings_compat

_graphiti_settings_compat.install()
