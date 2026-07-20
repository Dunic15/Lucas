"""Compatibility settings for the optional Graphiti integration.

A recent main-branch merge kept the Graphiti client/UI but dropped its Settings
fields while adding the browser meeting trigger.  Keep production environment
configuration and the key-free test contract working until the fields are folded
back into config.py in a dedicated cleanup.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from .config import Settings, settings


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(raw: str) -> int:
    return int(raw.strip())


def _as_float(raw: str) -> float:
    return float(raw.strip())


_SPECS: dict[str, tuple[Any, Callable[[str], Any]]] = {
    "graphiti_enabled": (False, _as_bool),
    "graphiti_backend": ("neo4j", str),
    "graphiti_uri": ("", str),
    "graphiti_user": ("neo4j", str),
    "graphiti_password": ("", str),
    "graphiti_recall_timeout_s": (1.0, _as_float),
    "graphiti_recall_results": (8, _as_int),
    "graphiti_ingest_timeout_s": (20.0, _as_float),
}


def _value_from_env(name: str, default: Any, parser: Callable[[str], Any]) -> Any:
    raw = os.getenv(name.upper())
    if raw is None:
        return default
    try:
        return parser(raw)
    except (TypeError, ValueError):
        return default


def install() -> None:
    """Add property-backed fields only when the canonical model lacks them."""
    for name, (default, parser) in _SPECS.items():
        if name in Settings.model_fields or isinstance(getattr(Settings, name, None), property):
            continue
        storage = f"_laura_compat_{name}"

        def getter(self, *, _storage=storage, _default=default):
            return self.__dict__.get(_storage, _default)

        def setter(self, value, *, _storage=storage):
            object.__setattr__(self, _storage, value)

        setattr(Settings, name, property(getter, setter))
        setattr(settings, name, _value_from_env(name, default, parser))


def reset_defaults() -> None:
    """Restore key-free defaults between tests; production never calls this."""
    install()
    for name, (default, _parser) in _SPECS.items():
        setattr(settings, name, default)


install()
