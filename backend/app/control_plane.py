"""Durable identity / control plane — the Postgres DAL for self-serve signups.

Active **iff** ``settings.laura_database_url`` is non-empty (the Supabase
Postgres control plane of docs/infra/MULTI-TENANCY.md). When the URL is empty
— the key-free demo and the whole test suite — every public function here is a
no-op returning ``None``/``False`` and **no engine is ever created**, so the
SQLite runtime paths are byte-identical to today.

What lives here vs. SQLite (store.py):
- SQLite remains the RUNTIME store: sessions, utterances, routes, artifacts —
  the live-meeting hot path NEVER touches this module (latency is the product;
  control-plane calls happen on login/start/dashboard paths only).
- Postgres holds the DURABLE identity + billing spine: users (google_sub),
  orgs (UUID personal/domain orgs), memberships, org_agents grants, org_tokens
  (per-org machine bearers) and billing_accounts (plan + included_seconds) —
  the rows a redeploy must not wipe. store.py's tables become a warm cache.

ROLE / RLS CONTRACT:
``LAURA_DATABASE_URL`` is runtime-only and MUST authenticate as the dedicated
``laura_app`` role (NOSUPERUSER, NOBYPASSRLS). Alembic uses the separate
``LAURA_DATABASE_ADMIN_URL``; the owner credential is never available to
App Runner.

Tenant CRUD — including mint/rotate/revoke of Cedric machine tokens — stays on short transactions with transaction-local
``app.current_org``, so FORCE RLS is the database backstop. The two operations
whose tenant is the answer rather than an input — Google signup/domain
provisioning and token-hash-to-org resolution — call narrowly scoped
``SECURITY DEFINER`` functions in the non-exposed ``laura_private`` schema.
Migration 0004 pins their search_path, fully qualifies every object, revokes
PUBLIC/anon/authenticated, and grants only exact EXECUTE to ``laura_app``.

Nothing here ever logs emails, tokens, or any transcript/PII.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid
from typing import Any, Optional

from .config import settings

# Providers/statuses mirror the SQLite org_connections contract (store.py is
# the source of truth so the durable mirror can never drift from the runtime
# validation).
from .store import CONNECTION_PROVIDERS, CONNECTION_STATUSES

_LOCK = threading.Lock()
_engine = None
_engine_url: str = ""
_RUNTIME_ROLE = "laura_app"


def enabled() -> bool:
    """True when a control-plane database is configured."""
    return bool((settings.laura_database_url or "").strip())


def _sqlalchemy_url(raw: str) -> str:
    """Normalize a Postgres URL to the psycopg3 SQLAlchemy dialect. Supabase
    hands out ``postgresql://`` (which SQLAlchemy would route to psycopg2 —
    not installed); requirements.txt ships psycopg[binary] v3."""
    url = raw.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _assert_runtime_role(engine) -> dict:
    """Fail closed unless this engine is the exact policy-bound runtime role.

    The error is intentionally credential/URL-free. It protects every caller,
    including health probes that force engine creation, from an accidentally
    injected owner or BYPASSRLS DSN.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).fetchone()
    safe = (
        row is not None
        and str(row[0]) == _RUNTIME_ROLE
        and row[1] is False
        and row[2] is False
    )
    if not safe:
        raise RuntimeError(
            "unsafe runtime database role; expected laura_app with "
            "NOSUPERUSER NOBYPASSRLS"
        )
    return {
        "current_user": str(row[0]),
        "rolsuper": bool(row[1]),
        "rolbypassrls": bool(row[2]),
    }


def _get_engine():
    """Lazily create and role-verify the runtime engine before publishing it."""
    global _engine, _engine_url
    url = _sqlalchemy_url(settings.laura_database_url or "")
    if not url:
        return None
    with _LOCK:
        if _engine is None or _engine_url != url:
            from sqlalchemy import create_engine

            candidate = create_engine(url, pool_pre_ping=True)
            try:
                _assert_runtime_role(candidate)
            except Exception:
                candidate.dispose()
                raise
            if _engine is not None:
                _engine.dispose()
            _engine = candidate
            _engine_url = url
        return _engine


def runtime_role_status() -> Optional[dict]:
    """Health/preflight proof for the configured runtime connection."""
    engine = _get_engine()
    return _assert_runtime_role(engine) if engine is not None else None


def reset_engine() -> None:
    """Dispose the cached engine (tests switching databases)."""
    global _engine, _engine_url
    with _LOCK:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _engine_url = ""


def _set_org(conn, org_id: str) -> None:
    """Pin the transaction to one tenant so RLS policies apply to what follows.
    ``true`` = transaction-local: the setting dies with the txn, so pooled
    connections never leak an org context to the next checkout."""
    from sqlalchemy import text

    conn.execute(
        text("SELECT set_config('app.current_org', :org, true)"), {"org": org_id}
    )


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ── durable signup ─────────────────────────────────────────────────────

def ensure_user(
    google_sub: str, email: str, name: str = "", picture: str = ""
) -> Optional[dict]:
    """Find or provision the durable Google identity and its org.

    The tenant is not known until this lookup finishes, so runtime never reads
    ``users`` or ``org_domains`` directly. Migration 0004 exposes one
    exact, server-only SECURITY DEFINER entry point which performs the
    idempotent identity/domain/personal-org bundle and returns its resolved
    tenant. Raw profile fields are never logged.
    """
    if not enabled():
        return None
    email = (email or "").strip().lower()
    google_sub = (google_sub or "").strip()
    if not email:
        return None

    from sqlalchemy import text

    engine = _get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT user_id, org_id, normalized_email, created "
                "FROM laura_private.ensure_user(:sub, :email, :name)"
            ),
            {"sub": google_sub, "email": email, "name": name or ""},
        ).fetchone()
    if row is None:
        return None
    return {
        "user_id": str(row[0]),
        "org_id": str(row[1]),
        "email": str(row[2]),
        "created": bool(row[3]),
    }


# ── per-org machine tokens ─────────────────────────────────────────────

def resolve_org_token(raw_token: str) -> Optional[str]:
    """Resolve a raw machine bearer to its org without granting table access.

    The runtime sends only sha256(raw) into the exact
    ``laura_private.resolve_org_token`` SECURITY DEFINER function; the raw
    bearer is never stored or logged.
    """
    if not enabled():
        return None
    raw = (raw_token or "").strip()
    if not raw:
        return None

    from sqlalchemy import text

    engine = _get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT laura_private.resolve_org_token(:token_hash)"),
            {"token_hash": _hash_token(raw)},
        ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def mint_org_token(org_id: str, label: str = "") -> Optional[str]:
    """Mint a per-org machine bearer: store sha256(raw), return the raw ONCE
    (used by provisioning / PR D). None when disabled or org_id is empty."""
    if not enabled() or not (org_id or "").strip():
        return None
    from sqlalchemy import text

    raw = secrets.token_urlsafe(32)
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org_id.strip())
        conn.execute(
            text(
                "INSERT INTO org_tokens (token_hash, org_id, label) "
                "VALUES (:h, :o, :l)"
            ),
            {"h": _hash_token(raw), "o": org_id.strip(), "l": (label or "")[:80]},
        )
    return raw


def rotate_org_token(org_id: str, label: str) -> Optional[str]:
    """Atomically replace one labelled per-org bearer and return it once."""
    if not enabled() or not (org_id or "").strip() or not (label or "").strip():
        return None
    from sqlalchemy import text

    org = org_id.strip()
    token_label = label.strip()[:80]
    raw = secrets.token_urlsafe(32)
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        conn.execute(
            text("DELETE FROM org_tokens WHERE org_id = :o AND label = :l"),
            {"o": org, "l": token_label},
        )
        conn.execute(
            text(
                "INSERT INTO org_tokens (token_hash, org_id, label) "
                "VALUES (:h, :o, :l)"
            ),
            {"h": _hash_token(raw), "o": org, "l": token_label},
        )
    return raw


def revoke_org_tokens(org_id: str, label: str = "") -> bool:
    """Revoke an org's machine bearers, optionally restricted to one label."""
    if not enabled() or not (org_id or "").strip():
        return False
    from sqlalchemy import text

    org = org_id.strip()
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        if (label or "").strip():
            conn.execute(
                text("DELETE FROM org_tokens WHERE org_id = :o AND label = :l"),
                {"o": org, "l": label.strip()[:80]},
            )
        else:
            conn.execute(
                text("DELETE FROM org_tokens WHERE org_id = :o"), {"o": org}
            )
    return True


# ── durable org_connections mirror + billing plan ──────────────────────

def set_connection(
    org_id: str,
    avatar_id: str,
    provider: str,
    status: str,
    config: dict | None = None,
) -> Optional[bool]:
    """Durable upsert of one (org, avatar, provider) connection — the Postgres
    mirror of store.set_connection (same validation; config is NON-SECRET
    wiring only). None when disabled; False on invalid input; True on write."""
    if not enabled():
        return None
    if (
        not (org_id or "").strip()
        or not (avatar_id or "").strip()
        or provider not in CONNECTION_PROVIDERS
        or status not in CONNECTION_STATUSES
    ):
        return False
    from sqlalchemy import text

    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org_id.strip())
        conn.execute(
            text(
                """
                INSERT INTO org_connections
                    (org_id, avatar_id, provider, status, config_json, updated_at)
                VALUES (:o, :a, :p, :s, :c, now())
                ON CONFLICT (org_id, avatar_id, provider) DO UPDATE SET
                    status = excluded.status,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """
            ),
            {
                "o": org_id.strip(),
                "a": avatar_id.strip(),
                "p": provider,
                "s": status,
                "c": json.dumps(config or {}),
            },
        )
    return True


def get_connections(org_id: str) -> Optional[list[dict[str, Any]]]:
    """Every durable connection row for an org (config parsed) — the Postgres
    mirror of store.connections_for_org. None when disabled."""
    if not enabled():
        return None
    if not (org_id or "").strip():
        return []
    from sqlalchemy import text

    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org_id.strip())
        rows = conn.execute(
            text(
                "SELECT avatar_id, provider, status, config_json, "
                "extract(epoch from updated_at) AS updated_at "
                "FROM org_connections WHERE org_id = :o"
            ),
            {"o": org_id.strip()},
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            config = json.loads(r[3]) if r[3] else {}
        except ValueError:
            config = {}
        out.append(
            {
                "avatar_id": r[0],
                "provider": r[1],
                "status": r[2],
                "config": config,
                "updated_at": float(r[4] or 0) or time.time(),
            }
        )
    return out


def org_plan(org_id: str) -> Optional[dict]:
    """``{plan, included_seconds}`` for an org's billing account (PR B's trial
    enforcement reads this), or None when disabled / no billing row."""
    if not enabled() or not (org_id or "").strip():
        return None
    from sqlalchemy import text

    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org_id.strip())
        row = conn.execute(
            text(
                "SELECT plan, included_seconds FROM billing_accounts "
                "WHERE org_id = :o"
            ),
            {"o": org_id.strip()},
        ).fetchone()
    if row is None:
        return None
    return {"plan": str(row[0]), "included_seconds": int(row[1])}
