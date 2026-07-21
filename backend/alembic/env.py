"""Alembic environment for Laura's control plane.

Key property: migrations use **only** LAURA_DATABASE_ADMIN_URL. The runtime
LAURA_DATABASE_URL is deliberately ignored here: it belongs to the policy-bound
`laura_app` role and must never be promoted into a DDL/owner credential. When
the admin URL is empty Alembic is a clean no-op, preserving the key-free demo.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Make the app package importable (backend/ on sys.path) so we can read the URL
# from the single source of truth, config.Settings.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # logging config is best-effort, never fatal
        pass

# No autogenerate: 0001 is hand-written raw DDL (RLS/policies live outside the
# SQLAlchemy metadata), so target_metadata stays None.
target_metadata = None

DATABASE_URL = (settings.laura_database_admin_url or "").strip()
# Same dialect normalization as control_plane._engine(): Supabase hands out
# `postgresql://`, which SQLAlchemy routes to the UNinstalled psycopg2 driver;
# we ship psycopg (v3), so pin the dialect explicitly.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import create_engine

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if not DATABASE_URL:
    runtime_configured = bool((settings.laura_database_url or "").strip())
    migration_required = bool(settings.laura_require_migrations)
    if runtime_configured or migration_required:
        # A configured runtime with no migration owner is a production
        # misconfiguration, not a demo. Exit non-zero before attempting any
        # connection and never echo either URL.
        raise RuntimeError(
            "LAURA_DATABASE_ADMIN_URL is required for this configured "
            "runtime/migration job; refusing to skip migrations"
        )
    # Both URLs empty is the intentional key-free SQLite demo.
    print(
        "Both database URLs are empty; key-free SQLite demo runs no "
        "control-plane migrations. Skipping."
    )
elif context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
