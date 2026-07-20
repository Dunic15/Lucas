"""Compatibility shim: embeddings moved to app.brain.embeddings."""
import sys as _sys
from .brain import embeddings as _mod
_sys.modules[__name__]=_mod
