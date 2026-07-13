"""Durable identity / control plane — the Postgres DAL for self-serve signups.

Active **iff** ``settings.laura_database_url`` is non-empty (the Supabase
Postgres control plane of docs/infra/MULTI-TENANCY.md). When the URL is empty
— the key-free demo and the whole test suite — every public function here is a
no-op returning ``None``/``False`` and **no engine is ever created**, so the
SQLite runtime paths are byte-identical to today.

What lives here vs. SQLite (store.py):
- SQLite remains the live RUNTIME store for sessions, utterances, and routes.
  The live-meeting utterance hot path NEVER touches this module (latency is the
  product).
- Postgres holds the DURABLE identity + billing spine and the private completed
  meeting artifacts customers expect after a redeploy. Artifact calls happen
  only on finalize/archive/dashboard paths; store.py keeps a warm local cache.

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
            except RuntimeError:
                candidate.dispose()
                raise
            except Exception:
                candidate.dispose()
                # Startup/health logs must never render a DSN, host, username,
                # or password from a failed SQLAlchemy/driver connection.
                raise RuntimeError(
                    "runtime database role verification failed"
                ) from None
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


def set_org_token(org_id: str, label: str, raw_token: str) -> bool:
    """Atomically replace one labelled bearer with SHA-256(raw)."""
    if (
        not enabled()
        or not (org_id or "").strip()
        or not (label or "").strip()
        or not (raw_token or "").strip()
    ):
        return False
    from sqlalchemy import text

    org = org_id.strip()
    token_label = label.strip()[:80]
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
            {"h": _hash_token(raw_token.strip()), "o": org, "l": token_label},
        )
    return True


def _brain_install_lock(conn: Any, org_id: str, avatar_id: str = "") -> None:
    """Serialize every Cedric credential mutation for one organization.

    The webhook secret, peer bearer and Laura org-token label are org-wide, so
    an avatar-scoped lock is too narrow: a callback for one avatar could race a
    disconnect or reinstall initiated from another.  The transaction-scoped
    advisory lock also protects the no-row case.
    """
    from sqlalchemy import text

    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"laura-brain-credentials:{org_id}"},
    )


def begin_brain_install(
    org_id: str, avatar_id: str, nonce: str, channel: str = ""
) -> bool:
    """Persist the only nonce the next OAuth completion may claim."""
    if (
        not enabled()
        or not (org_id or "").strip()
        or not (avatar_id or "").strip()
        or not (nonce or "").strip()
    ):
        return False
    from sqlalchemy import text

    org = org_id.strip()
    avatar = avatar_id.strip()
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        _brain_install_lock(conn, org, avatar)
        row = conn.execute(
            text(
                "SELECT status, config_json FROM org_connections "
                "WHERE org_id = :o AND avatar_id = :a "
                "AND provider = 'cedric-brain' FOR UPDATE"
            ),
            {"o": org, "a": avatar},
        ).fetchone()
        config: dict[str, Any] = {}
        status = "pending"
        if row is not None:
            current_status = str(row[0] or "")
            # An explicit user install supersedes a disconnect that was started
            # but never finished: leaving the row at "disconnecting" would lock
            # every retry at 503 (a re-tested account could never reconnect). We
            # already hold the per-(org, avatar) install lock, so a real in-flight
            # disconnect is serialized before us — reset to "pending" and proceed;
            # the fresh nonce below is the new completion authority.
            status = "connected" if current_status == "connected" else "pending"
            try:
                config = json.loads(row[1]) if row[1] else {}
            except ValueError:
                config = {}
            if not isinstance(config, dict):
                config = {}
        config.pop("install_tombstone", None)
        config.pop("revoked_install_nonce", None)
        config["pending_install_nonce"] = nonce.strip()
        config["channel"] = (channel or "").strip()
        conn.execute(
            text(
                """
                INSERT INTO org_connections
                    (org_id, avatar_id, provider, status, config_json, updated_at)
                VALUES (:o, :a, 'cedric-brain', :s, :c, now())
                ON CONFLICT (org_id, avatar_id, provider) DO UPDATE SET
                    status = excluded.status,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """
            ),
            {"o": org, "a": avatar, "s": status, "c": json.dumps(config)},
        )
    return True


def accept_brain_install(
    org_id: str,
    avatar_id: str,
    nonce: str,
    raw_token: str,
    team_id: str,
    channel: str,
    webhook_secret: str,
    webhook_token: str,
) -> Optional[str]:
    """CAS one completion and bind retries to its exact accepted envelope."""
    if not enabled():
        return None
    org = (org_id or "").strip()
    avatar = (avatar_id or "").strip()
    install_nonce = (nonce or "").strip()
    raw = (raw_token or "").strip()
    team = (team_id or "").strip()
    callback_secret = (webhook_secret or "").strip()
    # Optional: Cedric's shipped install callback sends webhook_secret only.
    # An empty per-org bearer completes the install (tenancy = per-org HMAC);
    # the shared bearer covers Laura->Cedric until Option A lands.
    callback_token = (webhook_token or "").strip()
    callback_channel = (channel or "").strip()
    if not all((org, avatar, install_nonce, raw, team, callback_secret)):
        return "invalid"
    from sqlalchemy import text

    secret_hash = _hash_token(callback_secret)
    peer_hash = _hash_token(callback_token)
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        _brain_install_lock(conn, org, avatar)
        row = conn.execute(
            text(
                "SELECT status, config_json FROM org_connections "
                "WHERE org_id = :o AND avatar_id = :a "
                "AND provider = 'cedric-brain' FOR UPDATE"
            ),
            {"o": org, "a": avatar},
        ).fetchone()
        config: dict[str, Any] = {}
        current_status = ""
        if row is not None:
            current_status = str(row[0] or "")
            try:
                config = json.loads(row[1]) if row[1] else {}
            except ValueError:
                config = {}
            if not isinstance(config, dict):
                config = {}
        if current_status in ("disconnecting", "disconnected") or bool(
            config.get("install_tombstone")
        ):
            return "stale"

        installed = str(config.get("install_nonce") or "")
        pending = str(config.get("pending_install_nonce") or "")
        if pending:
            if pending != install_nonce:
                return "stale"
        elif installed == install_nonce:
            expected = (
                str(config.get("team_id") or ""),
                str(config.get("channel") or ""),
                str(config.get("webhook_secret_sha256") or ""),
                str(config.get("webhook_token_sha256") or ""),
            )
            presented = (team, callback_channel, secret_hash, peer_hash)
            return "replay" if expected == presented else "conflict"
        elif installed:
            return "stale"

        conn.execute(
            text(
                "DELETE FROM org_tokens "
                "WHERE org_id = :o AND label = 'cedric-slack-install'"
            ),
            {"o": org},
        )
        conn.execute(
            text(
                "INSERT INTO org_tokens (token_hash, org_id, label) "
                "VALUES (:h, :o, 'cedric-slack-install')"
            ),
            {"h": _hash_token(raw), "o": org},
        )
        config.update(
            {
                "team_id": team,
                "channel": callback_channel,
                "install_nonce": install_nonce,
                "webhook_secret_sha256": secret_hash,
                "webhook_token_sha256": peer_hash,
            }
        )
        config.pop("pending_install_nonce", None)
        config.pop("install_tombstone", None)
        config.pop("revoked_install_nonce", None)
        conn.execute(
            text(
                """
                INSERT INTO org_connections
                    (org_id, avatar_id, provider, status, config_json, updated_at)
                VALUES (:o, :a, 'cedric-brain', 'pending', :c, now())
                ON CONFLICT (org_id, avatar_id, provider) DO UPDATE SET
                    status = excluded.status,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """
            ),
            {"o": org, "a": avatar, "c": json.dumps(config)},
        )
    return "applied"


def complete_brain_install(
    org_id: str,
    avatar_id: str,
    nonce: str,
    raw_token: str,
    team_id: str,
    channel: str,
    webhook_secret: str,
    webhook_token: str,
) -> Optional[str]:
    """Atomically validate, sync the external registry and connect one install.

    The per-org advisory lock remains held while SSM is updated.  Therefore a
    newer install or disconnect cannot interpose between the compare-and-swap
    and the external write.  Database mutations happen only after the registry
    succeeds; a registry failure leaves the pending nonce retryable.
    """
    if not enabled():
        return None
    org = (org_id or "").strip()
    avatar = (avatar_id or "").strip()
    install_nonce = (nonce or "").strip()
    raw = (raw_token or "").strip()
    team = (team_id or "").strip()
    callback_secret = (webhook_secret or "").strip()
    # Optional: Cedric's shipped install callback sends webhook_secret only.
    # An empty per-org bearer completes the install (tenancy = per-org HMAC);
    # the shared bearer covers Laura->Cedric until Option A lands.
    callback_token = (webhook_token or "").strip()
    callback_channel = (channel or "").strip()
    if not all((org, avatar, install_nonce, raw, team, callback_secret)):
        return "invalid"

    from sqlalchemy import text
    from .cedric import secret_registry

    secret_hash = _hash_token(callback_secret)
    peer_hash = _hash_token(callback_token)
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        _brain_install_lock(conn, org)
        row = conn.execute(
            text(
                "SELECT status, config_json FROM org_connections "
                "WHERE org_id = :o AND avatar_id = :a "
                "AND provider = 'cedric-brain' FOR UPDATE"
            ),
            {"o": org, "a": avatar},
        ).fetchone()
        config: dict[str, Any] = {}
        current_status = ""
        if row is not None:
            current_status = str(row[0] or "")
            try:
                config = json.loads(row[1]) if row[1] else {}
            except ValueError:
                config = {}
            if not isinstance(config, dict):
                config = {}
        if current_status in ("disconnecting", "disconnected") or bool(
            config.get("install_tombstone")
        ):
            return "stale"

        installed = str(config.get("install_nonce") or "")
        pending = str(config.get("pending_install_nonce") or "")
        replay = False
        if pending:
            if pending != install_nonce:
                return "stale"
        elif installed == install_nonce:
            expected = (
                str(config.get("team_id") or ""),
                str(config.get("channel") or ""),
                str(config.get("webhook_secret_sha256") or ""),
                str(config.get("webhook_token_sha256") or ""),
            )
            presented = (team, callback_channel, secret_hash, peer_hash)
            if expected != presented:
                return "conflict"
            replay = True
        elif installed:
            return "stale"

        # This bounded network write deliberately occurs while the org-wide
        # transaction lock is held.  Provisioning is rare; correctness beats
        # releasing the lock and allowing stale SSM resurrection.
        if not secret_registry.upsert_org_credentials(
            org, callback_secret, callback_token
        ):
            return "registry_failed"

        conn.execute(
            text(
                "DELETE FROM org_tokens "
                "WHERE org_id = :o AND label = 'cedric-slack-install'"
            ),
            {"o": org},
        )
        conn.execute(
            text(
                "INSERT INTO org_tokens (token_hash, org_id, label) "
                "VALUES (:h, :o, 'cedric-slack-install')"
            ),
            {"h": _hash_token(raw), "o": org},
        )
        config.update(
            {
                "team_id": team,
                "channel": callback_channel,
                "install_nonce": install_nonce,
                "webhook_secret_sha256": secret_hash,
                "webhook_token_sha256": peer_hash,
            }
        )
        config.pop("pending_install_nonce", None)
        config.pop("install_tombstone", None)
        config.pop("revoked_install_nonce", None)
        config.pop("disconnect_phase", None)
        conn.execute(
            text(
                """
                INSERT INTO org_connections
                    (org_id, avatar_id, provider, status, config_json, updated_at)
                VALUES (:o, :a, 'cedric-brain', 'connected', :c, now())
                ON CONFLICT (org_id, avatar_id, provider) DO UPDATE SET
                    status = excluded.status,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """
            ),
            {"o": org, "a": avatar, "c": json.dumps(config)},
        )
    return "replay" if replay else "applied"


def finish_brain_install(org_id: str, avatar_id: str, nonce: str) -> Optional[str]:
    """Move one accepted install to connected under the install lock."""
    if (
        not enabled()
        or not (org_id or "").strip()
        or not (avatar_id or "").strip()
        or not (nonce or "").strip()
    ):
        return None
    from sqlalchemy import text

    org = org_id.strip()
    avatar = avatar_id.strip()
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        _brain_install_lock(conn, org, avatar)
        row = conn.execute(
            text(
                "SELECT status, config_json FROM org_connections "
                "WHERE org_id = :o AND avatar_id = :a "
                "AND provider = 'cedric-brain' FOR UPDATE"
            ),
            {"o": org, "a": avatar},
        ).fetchone()
        if row is None:
            return "stale"
        try:
            config = json.loads(row[1]) if row[1] else {}
        except ValueError:
            config = {}
        if not isinstance(config, dict):
            config = {}
        if str(row[0] or "") in ("disconnecting", "disconnected") or bool(
            config.get("install_tombstone")
        ):
            return "stale"
        # Any pending nonce was written by a newer /slack/start.  The legacy
        # split accept/finish helper must never connect the older install.
        if str(config.get("pending_install_nonce") or ""):
            return "stale"
        if str(config.get("install_nonce") or "") != nonce.strip():
            return "stale"
        conn.execute(
            text(
                "UPDATE org_connections SET status = 'connected', updated_at = now() "
                "WHERE org_id = :o AND avatar_id = :a "
                "AND provider = 'cedric-brain'"
            ),
            {"o": org, "a": avatar},
        )
    return "connected"


def begin_brain_disconnect(
    org_id: str, avatar_id: str, phase: str = "revoke_pending"
) -> bool:
    """Fence every brain row before org-wide remote/registry cleanup."""
    if (
        not enabled()
        or not (org_id or "").strip()
        or not (avatar_id or "").strip()
    ):
        return False
    from sqlalchemy import text

    org = org_id.strip()
    requested_avatar = avatar_id.strip()
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        _brain_install_lock(conn, org)
        rows = conn.execute(
            text(
                "SELECT avatar_id, config_json FROM org_connections "
                "WHERE org_id = :o AND provider = 'cedric-brain' FOR UPDATE"
            ),
            {"o": org},
        ).fetchall()
        if not rows or requested_avatar not in {str(row[0]) for row in rows}:
            return False
        # Fail closed at the START of disconnect. The Cedric→Laura bearer is
        # org-wide and must stop authenticating before remote/SSM cleanup can
        # block or fail.
        conn.execute(
            text(
                "DELETE FROM org_tokens "
                "WHERE org_id = :o AND label = 'cedric-slack-install'"
            ),
            {"o": org},
        )
        for row in rows:
            try:
                config = json.loads(row[1]) if row[1] else {}
            except ValueError:
                config = {}
            if not isinstance(config, dict):
                config = {}
            config["disconnect_phase"] = (phase or "revoke_pending").strip()
            conn.execute(
                text(
                    "UPDATE org_connections SET status = 'disconnecting', "
                    "config_json = :c, updated_at = now() "
                    "WHERE org_id = :o AND avatar_id = :a "
                    "AND provider = 'cedric-brain'"
                ),
                {
                    "o": org,
                    "a": str(row[0]),
                    "c": json.dumps(config),
                },
            )
    return True


def tombstone_brain_install(org_id: str, avatar_id: str) -> bool:
    """Revoke the org token and tombstone every brain row in one transaction."""
    if (
        not enabled()
        or not (org_id or "").strip()
        or not (avatar_id or "").strip()
    ):
        return False
    from sqlalchemy import text

    org = org_id.strip()
    requested_avatar = avatar_id.strip()
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        _brain_install_lock(conn, org)
        rows = conn.execute(
            text(
                "SELECT avatar_id, config_json FROM org_connections "
                "WHERE org_id = :o AND provider = 'cedric-brain' FOR UPDATE"
            ),
            {"o": org},
        ).fetchall()
        if not rows or requested_avatar not in {str(row[0]) for row in rows}:
            return False
        conn.execute(
            text(
                "DELETE FROM org_tokens "
                "WHERE org_id = :o AND label = 'cedric-slack-install'"
            ),
            {"o": org},
        )
        for row in rows:
            try:
                old = json.loads(row[1]) if row[1] else {}
            except ValueError:
                old = {}
            if not isinstance(old, dict):
                old = {}
            revoked_nonce = str(
                old.get("pending_install_nonce")
                or old.get("install_nonce")
                or old.get("revoked_install_nonce")
                or ""
            )
            tombstone: dict[str, Any] = {"install_tombstone": True}
            if revoked_nonce:
                tombstone["revoked_install_nonce"] = revoked_nonce
            conn.execute(
                text(
                    "UPDATE org_connections SET status = 'disconnected', "
                    "config_json = :c, updated_at = now() "
                    "WHERE org_id = :o AND avatar_id = :a "
                    "AND provider = 'cedric-brain'"
                ),
                {
                    "o": org,
                    "a": str(row[0]),
                    "c": json.dumps(tombstone),
                },
            )
    return True


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


_ARTIFACT_VISIBILITIES = frozenset({"participants", "org", "private"})


def _artifact_payload(value: Any) -> dict:
    """Normalize psycopg's jsonb result without ever rendering its PII."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def save_artifact(
    org_id: str,
    bot_id: str,
    artifact: dict,
    *,
    visibility: str = "participants",
    saved_at: float | None = None,
    delete_by: float | None = None,
) -> Optional[bool]:
    """Durably upsert one full meeting artifact inside one tenant.

    The payload intentionally includes the transcript: this is the private,
    RLS-protected customer archive, not a Cedric wire envelope. delete_by is
    stamped from the owning org's existing retention_days contract when the
    caller does not provide it. There is no delete worker or DELETE grant here.
    """
    if not enabled():
        return None
    org = (org_id or "").strip()
    bot = (bot_id or "").strip()
    if not org or not bot:
        raise ValueError("org_id and bot_id are required for durable artifacts")
    if not isinstance(artifact, dict):
        raise ValueError("artifact must be a JSON object")

    payload = dict(artifact)
    # The RLS column and private JSON must never disagree.
    payload["org_id"] = org
    saved_epoch = time.time() if saved_at is None else float(saved_at)
    row_visibility = (
        visibility if visibility in _ARTIFACT_VISIBILITIES else "participants"
    )

    from sqlalchemy import text

    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        retention_row = conn.execute(
            text(
                "SELECT retention_days FROM public.orgs "
                "WHERE id = CAST(:o AS uuid)"
            ),
            {"o": org},
        ).fetchone()
        if retention_row is None:
            raise RuntimeError("artifact owner org is unavailable")
        retention_days = max(0, int(retention_row[0]))
        delete_epoch = (
            saved_epoch + retention_days * 86400
            if delete_by is None
            else float(delete_by)
        )
        conn.execute(
            text(
                """
                INSERT INTO public.artifacts AS current
                    (org_id, bot_id, artifact, visibility, saved_at, delete_by)
                VALUES (
                    CAST(:o AS uuid), :b, CAST(:a AS jsonb), :v,
                    to_timestamp(CAST(:saved AS double precision)),
                    to_timestamp(CAST(:delete AS double precision))
                )
                ON CONFLICT (org_id, bot_id) DO UPDATE SET
                    artifact = excluded.artifact,
                    visibility = excluded.visibility,
                    saved_at = excluded.saved_at,
                    delete_by = excluded.delete_by
                """
            ),
            {
                "o": org,
                "b": bot,
                "a": json.dumps(payload),
                "v": row_visibility,
                "saved": saved_epoch,
                "delete": delete_epoch,
            },
        )
    return True


def get_artifact(org_id: str, bot_id: str) -> Optional[dict]:
    """Return one tenant's private artifact, never a global bot-id lookup."""
    if not enabled():
        return None
    org = (org_id or "").strip()
    bot = (bot_id or "").strip()
    if not org or not bot:
        return None

    from sqlalchemy import text

    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        row = conn.execute(
            text(
                "SELECT artifact FROM public.artifacts "
                "WHERE org_id = CAST(:o AS uuid) AND bot_id = :b"
            ),
            {"o": org, "b": bot},
        ).fetchone()
    return _artifact_payload(row[0]) if row is not None else None


def list_artifacts(org_id: str) -> Optional[list[dict[str, Any]]]:
    """List one tenant's private archive in the legacy dashboard row shape."""
    if not enabled():
        return None
    org = (org_id or "").strip()
    if not org:
        raise ValueError("org_id is required for durable artifact enumeration")

    from sqlalchemy import text

    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        rows = conn.execute(
            text(
                "SELECT bot_id, extract(epoch FROM saved_at), artifact "
                "FROM public.artifacts "
                "WHERE org_id = CAST(:o AS uuid) "
                "ORDER BY saved_at DESC, bot_id DESC"
            ),
            {"o": org},
        ).fetchall()
    return [
        {
            "bot_id": str(row[0]),
            "saved_at": float(row[1] or 0),
            "artifact": _artifact_payload(row[2]),
        }
        for row in rows
    ]


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


# ── Stripe billing control plane (PR C, migration 0005) ────────────────

_BILLING_TERMINAL = {
    "canceled", "unpaid", "incomplete_expired", "incomplete", "paused",
    "invalid", "none",
}
_BILLING_IRREVERSIBLE = {
    "canceled", "unpaid", "incomplete_expired", "invalid",
}


def billing_boundary_ready() -> bool:
    """Prove both the runtime role and migration 0005 private boundary."""
    if not enabled():
        return False
    from sqlalchemy import text

    engine = _get_engine()
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text("SELECT laura_private.billing_boundary_ready()")
            ).scalar()
        )


def _billing_dict(row) -> Optional[dict]:
    if row is None:
        return None
    return {
        "plan": str(row[0]),
        "included_seconds": int(row[1]),
        "subscription_status": str(row[2]),
        "current_period_start": int(row[3]) if row[3] is not None else None,
        "current_period_end": int(row[4]) if row[4] is not None else None,
        "stripe_customer_id": str(row[5]) if row[5] else None,
        "stripe_subscription_id": str(row[6]) if row[6] else None,
        "checkout_revision": int(row[7] or 0),
        "checkout_pending_until": int(row[8]) if row[8] is not None else None,
    }


def get_billing(org_id: str) -> Optional[dict]:
    """Tenant-scoped billing state. Customer ids are returned only for this org."""
    if not enabled() or not (org_id or "").strip():
        return None
    from sqlalchemy import text

    org = org_id.strip()
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        row = conn.execute(
            text(
                """
                SELECT plan, included_seconds, subscription_status,
                       EXTRACT(EPOCH FROM current_period_start),
                       EXTRACT(EPOCH FROM current_period_end),
                       stripe_customer_id, stripe_subscription_id,
                       checkout_revision,
                       EXTRACT(EPOCH FROM checkout_pending_until)
                  FROM billing_accounts
                 WHERE org_id = :o
                """
            ),
            {"o": org},
        ).fetchone()
    return _billing_dict(row)


def _is_uuid(value: str) -> bool:
    """The durable control plane keys on UUIDs; the ephemeral session layer
    keys personal identities on ``u_<hash>`` cache keys. Guard the uuid cast so
    a session-shaped id can never crash a billing lookup (would surface as 500)."""
    import uuid as _uuid

    try:
        _uuid.UUID(str(value).strip())
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def is_durable_org(org_id: str) -> bool:
    """True when this org is a durable control-plane tenant (a real UUID), not a
    personal session identity (``u_<hash>``). Durable writes cast org_id to uuid
    and RLS pins ``app.current_org::uuid``, so a session-shaped id must never
    reach a durable write — it fails the cast and, in the connect/disconnect
    paths, surfaces as a bogus "connection persistence failed" 503. Personal
    orgs fall back to the SQLite store (the pre-control-plane behavior). Mirrors
    the ``_is_uuid`` guard ``member_role`` already applies to reads."""
    return enabled() and _is_uuid(org_id)


def member_role(org_id: str, user_id: str) -> Optional[str]:
    """Return only this member's active role through the private boundary."""
    if (
        not enabled()
        or not (org_id or "").strip()
        or not (user_id or "").strip()
        or not _is_uuid(org_id)
        or not _is_uuid(user_id)
    ):
        return None
    from sqlalchemy import text

    engine = _get_engine()
    with engine.connect() as conn:
        role = conn.execute(
            text(
                "SELECT laura_private.billing_member_role("
                "CAST(:o AS uuid), CAST(:u AS uuid))"
            ),
            {"o": org_id.strip(), "u": user_id.strip()},
        ).scalar()
    return str(role) if role is not None else None


def reserve_checkout(org_id: str) -> Optional[dict]:
    """Serialize Checkout for one org and reserve one revision for a short TTL."""
    if not enabled() or not (org_id or "").strip():
        return None
    from sqlalchemy import text

    org = org_id.strip()
    ttl = max(30, int(settings.billing_checkout_reservation_seconds))
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        conn.execute(
            text(
                "INSERT INTO billing_accounts (org_id, plan, included_seconds) "
                "VALUES (:o, 'free', :inc) ON CONFLICT (org_id) DO NOTHING"
            ),
            {"o": org, "inc": int(settings.free_trial_seconds)},
        )
        row = conn.execute(
            text(
                """
                SELECT stripe_customer_id, stripe_subscription_id,
                       subscription_status, checkout_revision,
                       checkout_pending_until > now()
                  FROM billing_accounts
                 WHERE org_id = :o
                 FOR UPDATE
                """
            ),
            {"o": org},
        ).fetchone()
        if row is None:
            raise RuntimeError("billing account unavailable")
        customer = str(row[0]) if row[0] else None
        subscription = str(row[1]) if row[1] else None
        status = str(row[2] or "none").lower()
        if subscription and status not in _BILLING_TERMINAL:
            return {
                "ok": False,
                "reason": "subscription_exists",
                "customer_id": customer,
            }
        if bool(row[4]):
            return {"ok": False, "reason": "checkout_in_progress"}
        revision = int(row[3] or 0) + 1
        conn.execute(
            text(
                """
                UPDATE billing_accounts
                   SET checkout_revision = :r,
                       checkout_pending_until =
                         now() + (:ttl * interval '1 second'),
                       updated_at = now()
                 WHERE org_id = :o
                """
            ),
            {"o": org, "r": revision, "ttl": ttl},
        )
    return {
        "ok": True,
        "revision": revision,
        "customer_id": customer,
    }


def bind_stripe_customer(
    org_id: str, revision: int, customer_id: str
) -> bool:
    """Bind the first Stripe Customer exactly once; rebinding is forbidden."""
    if (
        not enabled()
        or not (org_id or "").strip()
        or not (customer_id or "").strip()
    ):
        return False
    from sqlalchemy import text

    org = org_id.strip()
    customer = customer_id.strip()
    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org)
        row = conn.execute(
            text(
                """
                SELECT stripe_customer_id, checkout_revision
                  FROM billing_accounts
                 WHERE org_id = :o
                 FOR UPDATE
                """
            ),
            {"o": org},
        ).fetchone()
        if row is None or int(row[1] or 0) != int(revision):
            return False
        existing = str(row[0]) if row[0] else None
        if existing is not None and existing != customer:
            return False
        conn.execute(
            text(
                """
                UPDATE billing_accounts
                   SET stripe_customer_id = COALESCE(stripe_customer_id, :c),
                       updated_at = now()
                 WHERE org_id = :o
                """
            ),
            {"o": org, "c": customer},
        )
    return True


def finish_checkout(
    org_id: str,
    revision: int,
    customer_id: str,
    session_id: str,
    expires_at: int,
) -> bool:
    """Clear only the matching reservation after Stripe created the session."""
    if not enabled() or not (org_id or "").strip():
        return False
    from sqlalchemy import text

    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org_id.strip())
        result = conn.execute(
            text(
                """
                UPDATE billing_accounts
                   SET checkout_pending_until =
                         to_timestamp(CAST(:expires AS double precision)),
                       last_checkout_session_id = :s,
                       updated_at = now()
                 WHERE org_id = :o
                   AND checkout_revision = :r
                   AND stripe_customer_id = :c
                """
            ),
            {
                "o": org_id.strip(),
                "r": int(revision),
                "c": customer_id.strip(),
                "s": session_id.strip() or None,
                "expires": int(expires_at),
            },
        )
    return result.rowcount == 1


def release_checkout(org_id: str, revision: int) -> None:
    """Release only this failed attempt; never clear a newer reservation."""
    if not enabled() or not (org_id or "").strip():
        return
    from sqlalchemy import text

    engine = _get_engine()
    with engine.begin() as conn:
        _set_org(conn, org_id.strip())
        conn.execute(
            text(
                "UPDATE billing_accounts SET checkout_pending_until = NULL "
                "WHERE org_id = :o AND checkout_revision = :r"
            ),
            {"o": org_id.strip(), "r": int(revision)},
        )


def _event_is_newer(created: int, event_id: str, old_created: int, old_id: str) -> bool:
    return (int(created), str(event_id)) > (int(old_created or 0), str(old_id or ""))


def apply_stripe_event(
    event_id: str,
    event_type: str,
    customer_id: str | None,
    effect: dict | None,
) -> Optional[bool]:
    """Claim and apply one signed Stripe event in one database transaction.

    The SECURITY DEFINER call can only insert the global event id and resolve
    one customer to one locked org. All state mutation after that is normal
    laura_app tenant CRUD under FORCE RLS. Any exception rolls both back, so
    Stripe receives a 5xx and retries.
    """
    if not enabled():
        return None
    from sqlalchemy import text

    engine = _get_engine()
    with engine.begin() as conn:
        claim = conn.execute(
            text(
                "SELECT claimed, resolved_org_id "
                "FROM laura_private.claim_billing_event(:e, :t, :c)"
            ),
            {
                "e": event_id,
                "t": event_type,
                "c": (customer_id or "").strip() or None,
            },
        ).fetchone()
        if claim is None or not bool(claim[0]):
            return False
        if effect is None:
            return True
        if claim[1] is None:
            raise RuntimeError("stripe customer is not bound")
        org = str(claim[1])
        asserted_org = str(effect.get("asserted_org") or "").strip()
        if asserted_org and asserted_org != org:
            return True

        _set_org(conn, org)
        row = conn.execute(
            text(
                """
                SELECT plan, included_seconds, subscription_status,
                       EXTRACT(EPOCH FROM current_period_start),
                       EXTRACT(EPOCH FROM current_period_end),
                       stripe_customer_id, stripe_subscription_id,
                       subscription_event_created, subscription_event_id,
                       invoice_event_created, invoice_event_id,
                       checkout_event_created, checkout_event_id,
                       verified_paid_subscription_id,
                       EXTRACT(EPOCH FROM verified_paid_period_start),
                       EXTRACT(EPOCH FROM verified_paid_period_end),
                       verified_paid_event_created, verified_paid_event_id,
                       invoice_event_subscription_id
                  FROM billing_accounts
                 WHERE org_id = :o
                 FOR UPDATE
                """
            ),
            {"o": org},
        ).fetchone()
        if row is None or str(row[5] or "") != str(customer_id or ""):
            raise RuntimeError("stripe customer binding changed")

        kind = str(effect.get("kind") or "")
        created = int(effect.get("event_created") or 0)
        current_sub = str(row[6] or "")
        current_status = str(row[2] or "none").lower()

        if kind == "checkout_link":
            if not _event_is_newer(created, event_id, int(row[11] or 0), str(row[12] or "")):
                return True
            conn.execute(
                text(
                    """
                    UPDATE billing_accounts
                       SET checkout_event_created = :created,
                           checkout_event_id = :event_id,
                           last_checkout_session_id = :session_id,
                           last_checkout_subscription_id = :subscription_id,
                           updated_at = now()
                     WHERE org_id = :o
                    """
                ),
                {
                    "o": org,
                    "created": created,
                    "event_id": event_id,
                    "session_id": effect.get("session_id") or None,
                    "subscription_id": effect.get("subscription_id") or None,
                },
            )
            return True

        if kind == "subscription":
            sub = str(effect.get("subscription_id") or "")
            status = str(effect.get("status") or "invalid").lower()
            access = str(effect.get("access") or "terminal")
            old_created = int(row[7] or 0)
            if not sub or created < old_created:
                return True
            if current_sub and sub != current_sub:
                if current_status not in _BILLING_TERMINAL or access == "terminal":
                    return True
            if (
                current_sub == sub
                and current_status in _BILLING_IRREVERSIBLE
                and access != "terminal"
            ):
                return True
            if created == old_created and current_sub == sub:
                priority = {"active": 1, "past_due": 2, "terminal": 3}
                current_access = (
                    "terminal"
                    if current_status in _BILLING_TERMINAL
                    else ("past_due" if current_status == "past_due" else "active")
                )
                if priority.get(access, 3) <= priority[current_access]:
                    return True
            if not bool(effect.get("price_valid")):
                if current_sub == sub:
                    access = "terminal"
                    status = "invalid"
                else:
                    return True

            proof_sub = str(row[13] or "")
            proof_start = int(row[14]) if row[14] is not None else None
            proof_end = int(row[15]) if row[15] is not None else None
            verified_window = bool(
                proof_sub == sub
                and proof_start is not None
                and proof_end is not None
                and proof_end > proof_start
            )
            if access == "terminal":
                plan = "free"
                included = int(settings.free_trial_seconds)
                stored_status = status or "canceled"
                period_start = None
                period_end = None
            elif access == "active" and verified_window:
                plan = "solo"
                included = int(settings.solo_included_seconds)
                stored_status = "active"
                period_start = proof_start
                period_end = proof_end
            elif (
                access == "past_due"
                and current_sub == sub
                and str(row[0]) == "solo"
                and row[3] is not None
                and row[4] is not None
            ):
                # A failed renewal never advances or resets the last paid
                # allowance window. Access lasts only through its paid end.
                plan = "solo"
                included = int(settings.solo_included_seconds)
                stored_status = "past_due"
                period_start = int(row[3])
                period_end = int(row[4])
            else:
                # Subscription state alone is not proof of payment. The exact
                # invoice.paid signal may arrive before or after this event.
                plan = "free"
                included = int(settings.free_trial_seconds)
                stored_status = (
                    "past_due" if access == "past_due" else "awaiting_payment"
                )
                period_start = None
                period_end = None

            conn.execute(
                text(
                    """
                    UPDATE billing_accounts
                       SET plan = :plan,
                           included_seconds = :included,
                           subscription_status = :status,
                           current_period_start =
                             CASE
                               WHEN CAST(:ps AS double precision) IS NULL
                                 THEN NULL
                               ELSE to_timestamp(
                                 CAST(:ps AS double precision)
                               )
                             END,
                           current_period_end =
                             CASE
                               WHEN CAST(:pe AS double precision) IS NULL
                                 THEN NULL
                               ELSE to_timestamp(
                                 CAST(:pe AS double precision)
                               )
                             END,
                           stripe_subscription_id = :sub,
                           subscription_event_created = :created,
                           subscription_event_id = :event_id,
                           checkout_pending_until = NULL,
                           updated_at = now()
                     WHERE org_id = :o
                    """
                ),
                {
                    "o": org,
                    "plan": plan,
                    "included": included,
                    "status": stored_status,
                    "ps": period_start,
                    "pe": period_end,
                    "sub": sub,
                    "created": created,
                    "event_id": event_id,
                },
            )
            return True

        if kind in {"invoice_paid", "invoice_failed"}:
            sub = str(effect.get("subscription_id") or "")
            old_invoice_sub = str(row[18] or "")
            if (
                not sub
                or not bool(effect.get("price_valid"))
                or (
                    old_invoice_sub == sub
                    and created < int(row[9] or 0)
                )
                or (
                    old_invoice_sub == sub
                    and created == int(row[9] or 0)
                    and kind != "invoice_failed"
                )
            ):
                return True

            params = {
                "o": org,
                "created": created,
                "event_id": event_id,
                "sub": sub,
            }
            if kind == "invoice_failed":
                if (
                    sub == current_sub
                    and current_status
                    in {"active", "past_due", "awaiting_payment"}
                ):
                    conn.execute(
                        text(
                            """
                            UPDATE billing_accounts
                               SET subscription_status = 'past_due',
                                   invoice_event_created = :created,
                                   invoice_event_id = :event_id,
                                   invoice_event_subscription_id = :sub,
                                   updated_at = now()
                             WHERE org_id = :o
                            """
                        ),
                        params,
                    )
                return True

            ps = effect.get("period_start")
            pe = effect.get("period_end")
            if (
                ps is None
                or pe is None
                or int(pe) <= int(ps)
            ):
                return True

            # Do not let an old subscription's invoice touch a newer active
            # subscription. A paid invoice may be remembered before its own
            # subscription event only when there is no current subscription or
            # the old one is terminal.
            if (
                current_sub
                and sub != current_sub
                and current_status not in _BILLING_TERMINAL
            ):
                return True
            if (
                current_status in _BILLING_TERMINAL
                and created < int(row[7] or 0)
            ):
                return True

            proof_sub = str(row[13] or "")
            proof_start = int(row[14]) if row[14] is not None else None
            proof_created = int(row[16] or 0)
            if proof_sub == sub and (
                int(ps) < int(proof_start or 0)
                or created < proof_created
            ):
                return True

            params.update(
                {
                    "ps": int(ps),
                    "pe": int(pe),
                    "sub": sub,
                    "adopt": bool(
                        not current_sub
                        or (
                            sub != current_sub
                            and current_status in _BILLING_TERMINAL
                        )
                    ),
                    "grant": bool(
                        sub == current_sub
                        and current_status
                        in {"active", "past_due", "awaiting_payment"}
                    ),
                    "solo": int(settings.solo_included_seconds),
                }
            )
            conn.execute(
                text(
                    """
                    UPDATE billing_accounts
                       SET verified_paid_subscription_id = :sub,
                           verified_paid_period_start =
                             to_timestamp(CAST(:ps AS double precision)),
                           verified_paid_period_end =
                             to_timestamp(CAST(:pe AS double precision)),
                           verified_paid_event_created = :created,
                           verified_paid_event_id = :event_id,
                           stripe_subscription_id =
                             CASE WHEN :adopt THEN :sub
                                  ELSE stripe_subscription_id END,
                           subscription_status =
                             CASE WHEN :grant THEN 'active'
                                  WHEN :adopt THEN 'awaiting_subscription'
                                  ELSE subscription_status END,
                           plan = CASE WHEN :grant THEN 'solo' ELSE plan END,
                           included_seconds =
                             CASE WHEN :grant THEN :solo
                                  ELSE included_seconds END,
                           current_period_start =
                             CASE WHEN :grant
                                  THEN to_timestamp(
                                    CAST(:ps AS double precision)
                                  )
                                  ELSE current_period_start END,
                           current_period_end =
                             CASE WHEN :grant
                                  THEN to_timestamp(
                                    CAST(:pe AS double precision)
                                  )
                                  ELSE current_period_end END,
                           invoice_event_created = :created,
                           invoice_event_id = :event_id,
                           invoice_event_subscription_id = :sub,
                           updated_at = now()
                     WHERE org_id = :o
                    """
                ),
                params,
            )
            return True

        return True
