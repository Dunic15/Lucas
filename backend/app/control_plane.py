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

ROLE / RLS CONTRACT (read this before pointing LAURA_DATABASE_URL anywhere):
Alembic 0001 puts FORCE ROW LEVEL SECURITY + a ``current_setting('app.
current_org')`` policy on the identity tables (orgs, memberships, org_domains,
org_agents, org_tokens) as well as the data-plane tables, and FORCE means even
the table owner is policy-bound. Two classes of operation live in this module:

1. **Tenant-scoped ops** (``set_connection`` / ``get_connections`` /
   ``org_plan`` / ``mint_org_token``, and every insert ``ensure_user`` makes
   for the org it is creating): these SET ``app.current_org`` on the
   connection (``SELECT set_config('app.current_org', :org, true)`` inside the
   transaction — txn-local) so RLS is *exercised*, not bypassed. For a brand
   new org the UUID is generated client-side first, so the bootstrap inserts
   satisfy the WITH CHECK policy even under a policy-enforcing role.
2. **Cross-tenant identity lookups** — finding a user by google_sub/email,
   resolving an email domain via ``org_domains``, and resolving a raw token
   via ``org_tokens`` (``resolve_org_token``) — are inherently org-less: the
   org is the *answer*, not an input. Under 0001's FORCE RLS these lookups
   return nothing for a plain policy-bound role. THEREFORE this module assumes
   the connection role is the **migration/owner role** (the same
   LAURA_DATABASE_URL used for ``alembic upgrade head`` — on Supabase the
   ``postgres`` role, which carries BYPASSRLS; in the pg test suite the
   embedded-Postgres superuser). RLS remains the safety net for the tenant
   DATA-plane and for any lower-privileged role (the ``laura_app`` pattern
   0001 provisions for): the two-org test suite proves the policies hold on
   the new tables for exactly such a role.

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


def _get_engine():
    """Lazily-created module-level engine (pool_pre_ping so a dropped pooler
    connection is replaced, not surfaced). Rebuilt if the URL changes (tests)."""
    global _engine, _engine_url
    url = _sqlalchemy_url(settings.laura_database_url or "")
    if not url:
        return None
    with _LOCK:
        if _engine is None or _engine_url != url:
            from sqlalchemy import create_engine

            if _engine is not None:
                _engine.dispose()
            _engine = create_engine(url, pool_pre_ping=True)
            _engine_url = url
        return _engine


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
    """The durable signup: find-or-create the user + their org. Idempotent —
    a re-login returns the same UUIDs.

    Lookup order: users.google_sub, then email (backfilling google_sub — a
    user who existed before sub tracking, or whose first login predates 0002).
    Org resolution mirrors ``store.org_id_for_email``: a VERIFIED domain in
    org_domains maps the login to THAT shared org (membership role 'member',
    no personal org is created); otherwise the user's existing owned personal
    org, else a brand new one — orgs row (name = email local-part, plan
    'free'), an 'owner' membership, org_agents grants for exactly
    ('laura','cedric'), and a billing_accounts row (plan 'free',
    included_seconds 900 — the 15-minute trial PR B enforces).

    Returns ``{user_id, org_id, email, created}`` (UUIDs as str; ``created``
    is True only when the users row was created now). ``None`` when the
    control plane is disabled.
    """
    if not enabled():
        return None
    from sqlalchemy import text

    email = (email or "").strip().lower()
    google_sub = (google_sub or "").strip()
    if not email:
        return None
    engine = _get_engine()

    with engine.begin() as conn:
        # 1. find the user (sub first, then email → backfill the sub).
        row = None
        if google_sub:
            row = conn.execute(
                text("SELECT id, email FROM users WHERE google_sub = :sub"),
                {"sub": google_sub},
            ).fetchone()
        if row is None:
            row = conn.execute(
                text("SELECT id, email FROM users WHERE email = :email"),
                {"email": email},
            ).fetchone()
            if row is not None and google_sub:
                conn.execute(
                    text(
                        "UPDATE users SET google_sub = :sub WHERE id = :id "
                        "AND (google_sub IS NULL OR google_sub = '')"
                    ),
                    {"sub": google_sub, "id": row[0]},
                )
        created = row is None
        if created:
            user_id = str(uuid.uuid4())
            # ON CONFLICT with NO arbiter: a concurrent same-person signup can
            # lose on EITHER unique constraint — users.email OR
            # uq_users_google_sub — and naming only (email) as the arbiter
            # left the sub collision raising IntegrityError (adversarial
            # review 2026-07-13; reproduced under a 12-thread race). The
            # arbiter-less DO NOTHING suppresses both; the winner re-read
            # below recovers either way. (Postgres parks the losing INSERT on
            # the speculative-insert lock until the winner's whole txn
            # commits, so the loser's re-read — and its owner-membership
            # lookup further down — always sees the winner's committed signup,
            # never a half-provisioned one.)
            conn.execute(
                text(
                    "INSERT INTO users (id, email, name, google_sub, provider, "
                    "auth_method) VALUES (:id, :email, :name, "
                    "NULLIF(:sub, ''), 'google', 'google') "
                    "ON CONFLICT DO NOTHING"
                ),
                {"id": user_id, "email": email, "name": name or "", "sub": google_sub},
            )
            # Re-read the winner: by sub first (a sub-constraint loss can sit
            # on a row whose email differs), then by email. Losing the race
            # means the row pre-existed → created False.
            got = None
            if google_sub:
                got = conn.execute(
                    text("SELECT id FROM users WHERE google_sub = :sub"),
                    {"sub": google_sub},
                ).fetchone()
            if got is None:
                got = conn.execute(
                    text("SELECT id FROM users WHERE email = :email"),
                    {"email": email},
                ).fetchone()
            created = str(got[0]) == user_id
            user_id = str(got[0])
        else:
            user_id = str(row[0])
            # Refresh the profile on re-login: name when Google sent one, and
            # ALWAYS the email — a sub-matched login is the same person even
            # after a Google-account address change (the sub is the durable
            # key; without this the row kept the signup-era email forever).
            # An email that moved to a DIFFERENT account would violate the
            # unique index and raise; upsert_user's caller falls back to
            # local resolution for that pathological case.
            conn.execute(
                text(
                    "UPDATE users SET "
                    "name = CASE WHEN :name <> '' THEN :name ELSE name END, "
                    "email = :email WHERE id = :id"
                ),
                {"name": name or "", "email": email, "id": user_id},
            )

        # 2. resolve the org: verified domain > existing owned org > new one.
        org_id = _resolve_domain_org(conn, email)
        if org_id is not None:
            _set_org(conn, org_id)
            conn.execute(
                text(
                    "INSERT INTO memberships (user_id, org_id, role, status) "
                    "VALUES (:u, :o, 'member', 'active') ON CONFLICT DO NOTHING"
                ),
                {"u": user_id, "o": org_id},
            )
        else:
            row = conn.execute(
                text(
                    "SELECT m.org_id FROM memberships m JOIN orgs o "
                    "ON o.id = m.org_id AND o.deleted_at IS NULL "
                    "WHERE m.user_id = :u AND m.role = 'owner' "
                    "ORDER BY o.created_at LIMIT 1"
                ),
                {"u": user_id},
            ).fetchone()
            if row is not None:
                org_id = str(row[0])
            else:
                org_id = _create_personal_org(conn, user_id, email)

    return {"user_id": user_id, "org_id": org_id, "email": email, "created": created}


def _resolve_domain_org(conn, email: str) -> Optional[str]:
    """org_id for the email's VERIFIED corporate domain, or None. Mirrors
    store.org_id_for_email: only rows with a non-null verified_at count, and
    free-mail domains are never in org_domains by contract."""
    from sqlalchemy import text

    _, _, domain = email.partition("@")
    if not domain:
        return None
    row = conn.execute(
        text(
            "SELECT org_id FROM org_domains "
            "WHERE domain = :d AND verified_at IS NOT NULL"
        ),
        {"d": domain},
    ).fetchone()
    return str(row[0]) if row else None


def _create_personal_org(conn, user_id: str, email: str) -> str:
    """Provision the personal org bundle: orgs row + owner membership +
    laura/cedric grants + the free-plan billing account. The org UUID is
    generated client-side and set as ``app.current_org`` FIRST, so every
    insert satisfies the WITH CHECK tenant policy even under FORCE RLS."""
    from sqlalchemy import text

    org_id = str(uuid.uuid4())
    _set_org(conn, org_id)
    local = email.partition("@")[0] or "personal"
    conn.execute(
        text(
            "INSERT INTO orgs (id, name, slug, plan) "
            "VALUES (:id, :name, :slug, 'free') ON CONFLICT (id) DO NOTHING"
        ),
        {"id": org_id, "name": local[:80], "slug": f"org-{uuid.UUID(org_id).hex[:12]}"},
    )
    conn.execute(
        text(
            "INSERT INTO memberships (user_id, org_id, role, status) "
            "VALUES (:u, :o, 'owner', 'active') ON CONFLICT DO NOTHING"
        ),
        {"u": user_id, "o": org_id},
    )
    for avatar_id in ("laura", "cedric"):
        conn.execute(
            text(
                "INSERT INTO org_agents (org_id, avatar_id) "
                "VALUES (:o, :a) ON CONFLICT DO NOTHING"
            ),
            {"o": org_id, "a": avatar_id},
        )
    conn.execute(
        text(
            "INSERT INTO billing_accounts (org_id, plan, included_seconds) "
            "VALUES (:o, 'free', 900) ON CONFLICT (org_id) DO NOTHING"
        ),
        {"o": org_id},
    )
    return org_id


# ── per-org machine tokens ─────────────────────────────────────────────

def resolve_org_token(raw_token: str) -> Optional[str]:
    """org_id owning this raw bearer (sha256 lookup in org_tokens), or None.
    Cross-tenant by nature (the org is the answer) — see the role contract in
    the module docstring. Never logs the token."""
    if not enabled():
        return None
    raw = (raw_token or "").strip()
    if not raw:
        return None
    from sqlalchemy import text

    engine = _get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT org_id FROM org_tokens WHERE token_hash = :h"),
            {"h": _hash_token(raw)},
        ).fetchone()
    return str(row[0]) if row else None


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
