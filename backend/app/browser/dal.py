"""Postgres DAL for the browser operator (B0); outbox_pg discipline.

Tenancy is NEVER derived from a provider session id or a client-supplied
org: every write pins ``app.current_org`` and FORCE RLS is the backstop.
``provider_ref`` is stored (the operator needs it to call the provider) but
is NEVER returned by ``public_view``: Laura's ``id`` uuid is the only public
identifier. Command idempotency is (org, session, command_id). Presentation
tokens store only the sha256 hash.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text

from .. import control_plane

_LIVE_STATES = ("creating", "ready", "presenting", "closing")
_TERMINAL_STATES = ("closed", "failed", "expired", "revoked")


def _engine():
    return control_plane._get_engine()


def _set_org(conn, org_id: str) -> None:
    control_plane._set_org(conn, org_id)


# ── sessions ────────────────────────────────────────────────────────────────

def create_session(
    org_id: str, *, principal: str, avatar_key: str, avatar_version: int,
    meeting_ref: str, provider: str, provider_ref: str, ttl_seconds: int,
    action_ref: str = "", metadata: dict | None = None,
) -> dict[str, Any]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                INSERT INTO browser_sessions (
                  org_id, principal, avatar_key, avatar_version, meeting_ref,
                  provider, provider_ref, state, action_ref, metadata_json,
                  expires_at
                ) VALUES (
                  :org_id, :principal, :avatar_key, :avatar_version,
                  :meeting_ref, :provider, :provider_ref, 'ready', :action_ref,
                  CAST(:metadata AS jsonb),
                  clock_timestamp() + (:ttl * interval '1 second')
                )
                RETURNING id::text, state, principal, avatar_key,
                          avatar_version, provider, meeting_ref,
                          last_command_seq, page_version, action_ref,
                          metadata_json,
                          extract(epoch from created_at) AS created_at,
                          extract(epoch from expires_at) AS expires_at
                """
            ),
            {"org_id": org_id, "principal": str(principal or "")[:120],
             "avatar_key": str(avatar_key or "")[:64],
             "avatar_version": int(avatar_version),
             "meeting_ref": str(meeting_ref or "")[:120],
             "provider": str(provider or "fake")[:32],
             "provider_ref": str(provider_ref or "")[:200],
             "action_ref": str(action_ref or "")[:64],
             "metadata": json.dumps(metadata or {}, separators=(",", ":"),
                                    default=str),
             "ttl": max(30, min(int(ttl_seconds), 24 * 3600))},
        ).mappings().one()
    return dict(row)


def _row_for_update(conn, org_id: str, session_id: str) -> Optional[dict]:
    row = conn.execute(
        text(
            """
            SELECT id::text, principal, avatar_key, avatar_version, provider,
                   provider_ref, meeting_ref, state, last_command_seq,
                   page_version, page_fingerprint, action_ref, metadata_json,
                   extract(epoch from created_at) AS created_at,
                   extract(epoch from expires_at) AS expires_at,
                   (expires_at <= clock_timestamp()) AS is_expired
            FROM browser_sessions
            WHERE org_id=:org_id AND id=CAST(:sid AS uuid)
            FOR UPDATE
            """
        ),
        {"org_id": org_id, "sid": session_id},
    ).mappings().first()
    return dict(row) if row else None


def get_session_internal(org_id: str, session_id: str) -> Optional[dict]:
    """Full row INCLUDING provider_ref; operator-internal only, lazily
    expiring a live-but-past-TTL session."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = _row_for_update(conn, org_id, session_id)
        if row is None:
            return None
        if row["is_expired"] and row["state"] in _LIVE_STATES:
            conn.execute(
                text(
                    "UPDATE browser_sessions SET state='expired', "
                    "updated_at=clock_timestamp() "
                    "WHERE org_id=:org_id AND id=CAST(:sid AS uuid)"
                ),
                {"org_id": org_id, "sid": session_id},
            )
            _revoke_tokens(conn, org_id, session_id)
            row["state"] = "expired"
    return row


def _num(value):
    """extract(epoch) returns Decimal: JSON-unsafe for Starlette. Coerce."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        f = float(value)
        return int(f) if f.is_integer() else f
    return value


def _as_dict(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    return value if isinstance(value, dict) else {}


def public_view(row: dict) -> dict:
    """The API-safe projection: NO provider_ref, NO provider viewer handle."""
    if row is None:
        return {}
    return {
        "id": row["id"],
        "state": row["state"],
        "avatar_key": row["avatar_key"],
        "avatar_version": int(row["avatar_version"]),
        "provider": row["provider"],
        "meeting_ref": row.get("meeting_ref", ""),
        "last_command_seq": int(row["last_command_seq"]),
        "page_version": int(row.get("page_version") or 0),
        "action_ref": row.get("action_ref", ""),
        "metadata": _as_dict(row.get("metadata_json")),
        "created_at": _num(row.get("created_at")),
        "expires_at": _num(row.get("expires_at")),
    }


def set_state(org_id: str, session_id: str, new_state: str) -> bool:
    """Transition state, but NEVER out of a terminal state (a concurrent
    close/revoke/expire is final; no path may resurrect it). The terminal
    guard is in the WHERE clause so it holds under concurrency, closing the
    check→act TOCTOU between get_session_internal and set_state."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                "UPDATE browser_sessions SET state=:state, "
                "updated_at=clock_timestamp() "
                "WHERE org_id=:org_id AND id=CAST(:sid AS uuid) "
                "AND state <> ALL(:terminal)"
            ),
            {"org_id": org_id, "sid": session_id, "state": new_state,
             "terminal": list(_TERMINAL_STATES)},
        )
        if new_state in _TERMINAL_STATES and result.rowcount:
            _revoke_tokens(conn, org_id, session_id)
    return bool(result.rowcount)


def transition(org_id: str, session_id: str, new_state: str,
               from_states: tuple[str, ...]) -> bool:
    """Atomic guarded transition: move to ``new_state`` ONLY if the current
    state is in ``from_states`` (single UPDATE, no read-then-write gap). This
    is the race-free primitive for present()'s ready→presenting move."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                "UPDATE browser_sessions SET state=:state, "
                "updated_at=clock_timestamp() "
                "WHERE org_id=:org_id AND id=CAST(:sid AS uuid) "
                "AND state = ANY(:from_states)"
            ),
            {"org_id": org_id, "sid": session_id, "state": new_state,
             "from_states": list(from_states)},
        )
        if new_state in _TERMINAL_STATES and result.rowcount:
            _revoke_tokens(conn, org_id, session_id)
    return bool(result.rowcount)


def sync_page_version(org_id: str, session_id: str,
                      fingerprint: str) -> tuple[int, bool]:
    """Advance page_version iff the page fingerprint changed since last sync.
    Deterministic + monotonic; the basis for visual verification and
    stale-page detection. Returns (page_version, changed)."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                UPDATE browser_sessions
                SET page_version = page_version
                    + CASE WHEN page_fingerprint <> :fp THEN 1 ELSE 0 END,
                    page_fingerprint = :fp,
                    updated_at = clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:sid AS uuid)
                RETURNING page_version,
                          (page_fingerprint IS DISTINCT FROM :fp) AS unused
                """
            ),
            {"org_id": org_id, "sid": session_id, "fp": str(fingerprint)[:128]},
        ).first()
    if row is None:
        return 0, False
    return int(row[0]), True


def set_metadata(org_id: str, session_id: str, metadata: dict) -> bool:
    """Replace the bounded, non-authoritative demo-run metadata blob."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                "UPDATE browser_sessions SET metadata_json=CAST(:m AS jsonb), "
                "updated_at=clock_timestamp() "
                "WHERE org_id=:org_id AND id=CAST(:sid AS uuid)"
            ),
            {"org_id": org_id, "sid": session_id,
             "m": json.dumps(metadata or {}, separators=(",", ":"),
                             default=str)},
        )
    return bool(result.rowcount)


def bump_seq(org_id: str, session_id: str) -> int:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                "UPDATE browser_sessions "
                "SET last_command_seq=last_command_seq+1, "
                "updated_at=clock_timestamp() "
                "WHERE org_id=:org_id AND id=CAST(:sid AS uuid) "
                "RETURNING last_command_seq"
            ),
            {"org_id": org_id, "sid": session_id},
        ).first()
    return int(row[0]) if row else 0


# ── command idempotency ─────────────────────────────────────────────────────

def recorded_command(org_id: str, session_id: str,
                     command_id: str) -> Optional[dict]:
    if not command_id:
        return None
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                "SELECT result_json FROM browser_commands "
                "WHERE org_id=:org_id AND session_id=CAST(:sid AS uuid) "
                "AND command_id=:cid"
            ),
            {"org_id": org_id, "sid": session_id, "cid": command_id},
        ).first()
    if row is None:
        return None
    result = row[0]
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {}
    return result if isinstance(result, dict) else {}


def claim_command(org_id: str, session_id: str, command_id: str,
                  verb: str) -> bool:
    """Atomically CLAIM a command_id before any work. Returns True for the
    single winner (who executes), False for a concurrent/replayed caller (who
    must return the recorded result). The seq/result are filled in by
    finalize_command. This is what makes concurrent same-command-id calls
    execute exactly once."""
    if not command_id:
        return True  # no idempotency requested; every call executes
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                INSERT INTO browser_commands
                  (org_id, session_id, command_id, seq, verb, result_json)
                VALUES (:org_id, CAST(:sid AS uuid), :cid, 0, :verb,
                        '{}'::jsonb)
                ON CONFLICT (org_id, session_id, command_id) DO NOTHING
                RETURNING command_id
                """
            ),
            {"org_id": org_id, "sid": session_id, "cid": command_id,
             "verb": str(verb or "")[:32]},
        ).first()
    return row is not None


def finalize_command(org_id: str, session_id: str, command_id: str, seq: int,
                     result: dict) -> None:
    """Write the winner's seq + result onto the claimed row."""
    if not command_id:
        return
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                "UPDATE browser_commands SET seq=:seq, "
                "result_json=CAST(:result AS jsonb) "
                "WHERE org_id=:org_id AND session_id=CAST(:sid AS uuid) "
                "AND command_id=:cid"
            ),
            {"org_id": org_id, "sid": session_id, "cid": command_id,
             "seq": int(seq),
             "result": json.dumps(result, separators=(",", ":"),
                                  default=str)},
        )


def durable_browser_action(org_id: str, action_id: str) -> Optional[dict]:
    """The durable queued_actions row for a browser guarded step; the
    trusted source the approve doors execute from (never the client body).
    Only route='browser' rows resolve here; RLS scopes to the caller's org."""
    if not action_id:
        return None
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT action_id, action, typed_json, risk, execution_route,
                       origin_avatar, permission_json
                FROM queued_actions
                WHERE org_id=:org_id AND action_id=:aid
                  AND execution_route='browser'
                """
            ),
            {"org_id": org_id, "aid": action_id},
        ).mappings().first()
    if row is None:
        return None

    def _obj(value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = {}
        return value if isinstance(value, dict) else {}

    out = dict(row)
    out["typed"] = _obj(out.pop("typed_json", None))
    out["permission_json"] = _obj(out.get("permission_json"))
    out["policy"] = "approval_required"
    return out


# ── presentation tokens ─────────────────────────────────────────────────────

def create_token(org_id: str, session_id: str, token_hash: str,
                 ttl_seconds: int) -> dict[str, Any]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                INSERT INTO browser_presentation_tokens
                  (org_id, session_id, token_hash, expires_at)
                VALUES (:org_id, CAST(:sid AS uuid), :hash,
                        clock_timestamp() + (:ttl * interval '1 second'))
                RETURNING id::text, extract(epoch from expires_at) AS expires_at
                """
            ),
            {"org_id": org_id, "sid": session_id, "hash": token_hash,
             "ttl": max(10, min(int(ttl_seconds), 3600))},
        ).mappings().one()
    return dict(row)


def redeem_token(org_id: str, token_hash: str) -> Optional[dict]:
    """Look up a token by hash for exchange: returns {session_id, valid,
    reason}. RLS scopes to the caller's org; a token from another org is
    simply not found. Marks the exchange (replay telemetry). Validity requires
    not-revoked, not-expired, AND a live session."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT t.id::text, t.session_id::text, t.revoked,
                       (t.expires_at <= clock_timestamp()) AS token_expired,
                       s.state,
                       (s.expires_at <= clock_timestamp()) AS session_expired
                FROM browser_presentation_tokens t
                JOIN browser_sessions s
                  ON s.org_id=t.org_id AND s.id=t.session_id
                WHERE t.org_id=:org_id AND t.token_hash=:hash
                FOR UPDATE OF t
                """
            ),
            {"org_id": org_id, "hash": token_hash},
        ).mappings().first()
        if row is None:
            return {"valid": False, "reason": "not_found", "session_id": ""}
        reason = ""
        if row["revoked"]:
            reason = "revoked"
        elif row["token_expired"]:
            reason = "expired"
        elif row["session_expired"] or row["state"] not in (
            "ready", "presenting"
        ):
            reason = "session_not_live"
        conn.execute(
            text(
                "UPDATE browser_presentation_tokens "
                "SET last_exchanged_at=clock_timestamp(), "
                "exchange_count=exchange_count+1 "
                "WHERE org_id=:org_id AND id=CAST(:tid AS uuid)"
            ),
            {"org_id": org_id, "tid": row["id"]},
        )
    return {"valid": not reason, "reason": reason,
            "session_id": row["session_id"]}


def revoke_token_by_hash(org_id: str, token_hash: str) -> bool:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                "UPDATE browser_presentation_tokens SET revoked=true "
                "WHERE org_id=:org_id AND token_hash=:hash AND NOT revoked"
            ),
            {"org_id": org_id, "hash": token_hash},
        )
    return bool(result.rowcount)


def _revoke_tokens(conn, org_id: str, session_id: str) -> None:
    conn.execute(
        text(
            "UPDATE browser_presentation_tokens SET revoked=true "
            "WHERE org_id=:org_id AND session_id=CAST(:sid AS uuid) "
            "AND NOT revoked"
        ),
        {"org_id": org_id, "sid": session_id},
    )


# ── expiry reconcile (lifespan worker) ──────────────────────────────────────

def expire_due(org_id: str, limit: int = 50) -> list[dict]:
    """Live sessions past TTL for one org; returns rows (with provider_ref)
    so the worker can close the provider, then marks them expired + revokes
    their tokens."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT id::text, provider, provider_ref
                FROM browser_sessions
                WHERE org_id=:org_id
                  AND state IN ('creating','ready','presenting','closing')
                  AND expires_at <= clock_timestamp()
                ORDER BY expires_at
                LIMIT :limit
                """
            ),
            {"org_id": org_id, "limit": max(1, min(int(limit), 200))},
        ).mappings().all()
        for r in rows:
            conn.execute(
                text(
                    "UPDATE browser_sessions SET state='expired', "
                    "updated_at=clock_timestamp() "
                    "WHERE org_id=:org_id AND id=CAST(:sid AS uuid)"
                ),
                {"org_id": org_id, "sid": r["id"]},
            )
            _revoke_tokens(conn, org_id, r["id"])
    return [dict(r) for r in rows]


def orgs_with_due_sessions(limit: int = 50) -> list[str]:
    """Cross-org discovery for the reconcile loop, via the private definer."""
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM laura_private.due_browser_orgs(:limit)"),
            {"limit": max(1, min(int(limit), 200))},
        ).fetchall()
    return [str(r[0]) for r in rows]


# ── browser identities (saved logins / provider contexts, 0014) ─────────────

def create_identity(org_id: str, *, label: str, provider: str,
                    context_ref: str) -> dict[str, Any]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                INSERT INTO browser_identities (
                  org_id, label, provider, context_ref
                ) VALUES (:org_id, :label, :provider, :context_ref)
                RETURNING id::text, label, provider, status,
                          extract(epoch from created_at)::float8 AS created_at
                """
            ),
            {"org_id": org_id, "label": label, "provider": provider,
             "context_ref": context_ref},
        ).mappings().first()
    return dict(row)


def identity_internal(org_id: str, label: str) -> Optional[dict[str, Any]]:
    """The ACTIVE identity for a label including context_ref; server-side
    only; context_ref never leaves through any public view."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT id::text, label, provider, context_ref, status
                  FROM browser_identities
                 WHERE org_id = :org_id AND label = :label
                   AND status = 'active'
                """
            ),
            {"org_id": org_id, "label": label},
        ).mappings().first()
    return dict(row) if row else None


def list_identities(org_id: str) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT id::text, label, provider, status,
                       extract(epoch from created_at)::float8 AS created_at
                  FROM browser_identities
                 WHERE org_id = :org_id AND status = 'active'
                 ORDER BY created_at DESC
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def revoke_identity(org_id: str, identity_id: str) -> bool:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                """
                UPDATE browser_identities
                   SET status = 'revoked', updated_at = now()
                 WHERE org_id = :org_id AND id = CAST(:iid AS uuid)
                   AND status = 'active'
                """
            ),
            {"org_id": org_id, "iid": identity_id},
        )
    return result.rowcount > 0
