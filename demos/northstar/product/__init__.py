"""Northstar demo product — isolated FastAPI app + deterministic seed/store.

Nothing in this package is imported by Laura's backend. It exists to be driven
by the eventual MVP integration branch and by this package's own tests.
"""
from .app import app  # noqa: F401  (convenience export for `uvicorn ...:app`)
