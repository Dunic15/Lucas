"""Postgres DAL for the Data Foundation (accepted contract v5).

The load-bearing semantics live here:

- ``commit_batch`` applies an ACCEPTED batch of envelopes AND advances the
  committed connector cursor in ONE transaction; a failed batch never moves
  the cursor (clarification 5's cursor-atomicity test rides this).
- Per (connector, external_id) an advisory transaction lock makes concurrent
  writers single-winner.
- Body-checksum dedupe never suppresses metadata/ACL/deletion changes: any
  such change creates a NEW immutable version (``lineage.meta_change_of``)
  and replaces the ACL rows, invalidating retrieval.
- ``acl_mode`` fail-closed: ``unknown`` stores ZERO acl rows; nothing -
  principal or meeting; can see the record.
- Quarantine backpressure: at 1000 open rows per (org, connector) the batch
  ABORTS with BackpressureError (the run parks); open rows are never evicted.
- Purges happen exclusively through the two SECURITY DEFINER functions; this
  module only calls them and completes the two-phase payload cleanup.

Nothing here logs record bodies, emails beyond errors, or credentials.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text

from .. import control_plane
from . import envelope as envelope_mod

ORG_SUBJECT = "org"


class BackpressureError(RuntimeError):
    """Open-quarantine cap reached; park the run, never delete data."""


class ScopeLostError(RuntimeError):
    """The connector lost its permission-reading scope mid-sync."""


def _engine():
    return control_plane._get_engine()


def _set_org(conn, org_id: str) -> None:
    control_plane._set_org(conn, org_id)


# ── connectors ──────────────────────────────────────────────────────────────

def create_connector(
    org_id: str, kind: str, name: str, *, config: dict | None = None,
    credential_ref: str = "", trusted_email_issuer: bool = False,
    actor: str = "",
) -> Optional[dict[str, Any]]:
    if kind not in ("upload", "gdrive", "slack", "notion", "crm", "custom"):
        return None
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                INSERT INTO df_connectors (
                  org_id, kind, name, config_json, credential_ref,
                  trusted_email_issuer, created_by, updated_by
                ) VALUES (
                  :org_id, :kind, :name, CAST(:config AS jsonb), :cred,
                  :trusted, :actor, :actor
                )
                RETURNING id::text, kind, name, status
                """
            ),
            {"org_id": org_id, "kind": kind,
             "name": str(name or kind).strip()[:120] or kind,
             "config": json.dumps(config or {}, separators=(",", ":"),
                                  sort_keys=True),
             "cred": str(credential_ref or "")[:200],
             "trusted": bool(trusted_email_issuer),
             "actor": str(actor or "")[:64]},
        ).mappings().one()
    return dict(row)


def get_connector(org_id: str, connector_id: str) -> Optional[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT c.id::text, c.kind, c.name, c.status, c.config_json,
                       (c.credential_ref <> '') AS has_credential,
                       c.trusted_email_issuer,
                       c.acl_mirrored,
                       extract(epoch from c.created_at) AS created_at,
                       (SELECT extract(epoch from max(r.updated_at))
                          FROM df_sync_runs r
                         WHERE r.org_id=c.org_id AND r.connector_id=c.id
                           AND r.status='done') AS last_success_at,
                       (SELECT count(*) FROM df_source_records s
                         WHERE s.org_id=c.org_id AND s.connector_id=c.id
                           AND NOT s.tombstoned) AS live_records,
                       (SELECT count(*) FROM df_quarantine q
                         WHERE q.org_id=c.org_id AND q.connector_id=c.id
                           AND q.state='open') AS open_quarantine,
                       (SELECT extract(epoch from k.advanced_at)
                          FROM df_connector_cursors k
                         WHERE k.org_id=c.org_id AND k.connector_id=c.id)
                         AS cursor_advanced_at
                FROM df_connectors c
                WHERE c.org_id=:org_id AND c.id=CAST(:cid AS uuid)
                """
            ),
            {"org_id": org_id, "cid": connector_id},
        ).mappings().first()
    if row is None:
        return None
    out = dict(row)
    cfg = out.get("config_json")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    out["config_json"] = cfg if isinstance(cfg, dict) else {}
    for key in ("live_records", "open_quarantine"):
        if out.get(key) is not None:
            out[key] = int(out[key])  # count(*) is Decimal. JSON-unsafe
    return out


def list_connectors(org_id: str) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT c.id::text, c.kind, c.name, c.status, c.acl_mirrored,
                       c.trusted_email_issuer,
                       extract(epoch from c.created_at) AS created_at,
                       (SELECT extract(epoch from max(r.updated_at))
                          FROM df_sync_runs r
                         WHERE r.org_id=c.org_id AND r.connector_id=c.id
                           AND r.status='done') AS last_success_at,
                       (SELECT count(*) FROM df_source_records s
                         WHERE s.org_id=c.org_id AND s.connector_id=c.id
                           AND NOT s.tombstoned) AS live_records,
                       (SELECT count(*) FROM df_quarantine q
                         WHERE q.org_id=c.org_id AND q.connector_id=c.id
                           AND q.state='open') AS open_quarantine
                FROM df_connectors c
                WHERE c.org_id=:org_id AND c.status <> 'revoked'
                ORDER BY c.created_at
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
    out = []
    for r in rows:
        item = dict(r)
        for key in ("live_records", "open_quarantine"):
            if item.get(key) is not None:
                item[key] = int(item[key])
        out.append(item)
    return out


def set_connector_status(org_id: str, connector_id: str, status: str,
                         actor: str = "") -> bool:
    if status not in ("active", "paused", "needs_reconnect",
                      "acl_incomplete", "revoked"):
        return False
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                """
                UPDATE df_connectors
                SET status=:status,
                    acl_mirrored=CASE WHEN :status IN
                      ('needs_reconnect', 'acl_incomplete')
                      THEN false ELSE acl_mirrored END,
                    updated_by=:actor, updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=CAST(:cid AS uuid)
                """
            ),
            {"org_id": org_id, "cid": connector_id, "status": status,
             "actor": str(actor or "")[:64]},
        )
    return bool(result.rowcount)


def set_connector_acl_mirrored(org_id: str, connector_id: str,
                               mirrored: bool) -> None:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        conn.execute(
            text(
                "UPDATE df_connectors SET acl_mirrored=:m, "
                "updated_at=clock_timestamp() "
                "WHERE org_id=:org_id AND id=CAST(:cid AS uuid)"
            ),
            {"org_id": org_id, "cid": connector_id, "m": bool(mirrored)},
        )


def ensure_connector(org_id: str, kind: str, name: str,
                     actor: str = "system", **kwargs) -> dict[str, Any]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                "SELECT id::text, kind, name, status FROM df_connectors "
                "WHERE org_id=:org_id AND kind=:kind AND status <> 'revoked' "
                "ORDER BY created_at LIMIT 1"
            ),
            {"org_id": org_id, "kind": kind},
        ).mappings().first()
    if row is not None:
        return dict(row)
    created = create_connector(org_id, kind, name, actor=actor, **kwargs)
    assert created is not None
    return created


# ── batch application (the atomic heart) ────────────────────────────────────

_QUARANTINE_OPEN_CAP = 1000


def _open_quarantine_count(conn, org_id: str, connector_id: str) -> int:
    row = conn.execute(
        text(
            "SELECT count(*) FROM df_quarantine "
            "WHERE org_id=:org_id AND connector_id=CAST(:cid AS uuid) "
            "AND state='open'"
        ),
        {"org_id": org_id, "cid": connector_id},
    ).first()
    return int(row[0])


def _quarantine(conn, org_id: str, connector_id: str,
                sync_run_id: int | None, external_id: str, reason: str,
                payload_ref: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO df_quarantine (org_id, connector_id, sync_run_id,
                                       external_id, reason, payload_ref)
            VALUES (:org_id, CAST(:cid AS uuid), :run_id, :eid, :reason,
                    :payload_ref)
            """
        ),
        {"org_id": org_id, "cid": connector_id, "run_id": sync_run_id,
         "eid": external_id[:300], "reason": reason[:200],
         "payload_ref": payload_ref[:200]},
    )


def _ensure_identity(conn, org_id: str, connector_id: str, external_id: str,
                     kind: str, *, display: str = "",
                     email_norm: str = "") -> str:
    row = conn.execute(
        text(
            """
            INSERT INTO df_identities (org_id, connector_id, external_id,
                                       kind, display, email_norm)
            VALUES (:org_id, CAST(:cid AS uuid), :eid, :kind, :display,
                    :email)
            ON CONFLICT (org_id, connector_id, external_id)
            DO UPDATE SET
              kind=excluded.kind,
              display=CASE WHEN excluded.display <> ''
                           THEN excluded.display
                           ELSE df_identities.display END,
              email_norm=CASE WHEN excluded.email_norm <> ''
                              THEN excluded.email_norm
                              ELSE df_identities.email_norm END,
              mirrored_at=clock_timestamp()
            RETURNING id::text
            """
        ),
        {"org_id": org_id, "cid": connector_id, "eid": external_id[:300],
         "kind": kind, "display": display[:200],
         "email": email_norm.lower()[:200]},
    ).mappings().one()
    return row["id"]


def _replace_acl(conn, org_id: str, connector_id: str, record_id: str,
                 acl_mode: str, entries: list[dict]) -> None:
    conn.execute(
        text(
            "DELETE FROM df_acl_entries "
            "WHERE org_id=:org_id AND record_id=CAST(:rid AS uuid)"
        ),
        {"org_id": org_id, "rid": record_id},
    )
    if acl_mode == "org_default":
        conn.execute(
            text(
                """
                INSERT INTO df_acl_entries
                  (org_id, record_id, subject_kind, identity_id, access)
                VALUES (:org_id, CAST(:rid AS uuid), 'org', NULL, 'reader')
                """
            ),
            {"org_id": org_id, "rid": record_id},
        )
        return
    if acl_mode != "mirrored":
        return  # unknown: FAIL-CLOSED; zero rows, visible to nobody
    seen: set[str] = set()
    for entry in entries:
        identity_id = _ensure_identity(
            conn, org_id, connector_id,
            entry["principal_external_id"],
            "group" if entry["principal_kind"] == "group" else "user",
        )
        if identity_id in seen:
            continue
        seen.add(identity_id)
        conn.execute(
            text(
                """
                INSERT INTO df_acl_entries
                  (org_id, record_id, subject_kind, identity_id, access)
                VALUES (:org_id, CAST(:rid AS uuid), 'identity',
                        CAST(:iid AS uuid), :access)
                """
            ),
            {"org_id": org_id, "rid": record_id, "iid": identity_id,
             "access": entry["access"]},
        )


def _current_version(conn, org_id: str, record: dict) -> Optional[dict]:
    if not record.get("current_version_id"):
        return None
    row = conn.execute(
        text(
            """
            SELECT id::text, version_no, title, mime, canonical_url,
                   author_external_id, body_checksum, body_ref
            FROM df_source_record_versions
            WHERE org_id=:org_id AND id=CAST(:vid AS uuid)
            """
        ),
        {"org_id": org_id, "vid": record["current_version_id"]},
    ).mappings().first()
    return dict(row) if row else None


def _acl_signature(conn, org_id: str, record_id: str) -> str:
    rows = conn.execute(
        text(
            """
            SELECT subject_kind, COALESCE(identity_id::text, ''), access
            FROM df_acl_entries
            WHERE org_id=:org_id AND record_id=CAST(:rid AS uuid)
            ORDER BY 1, 2, 3
            """
        ),
        {"org_id": org_id, "rid": record_id},
    ).fetchall()
    return "|".join(f"{a}:{b}:{c}" for a, b, c in rows)


def _apply_envelope(
    conn, org_id: str, connector_id: str, env: dict,
    sync_run_id: int | None,
) -> tuple[str, str]:
    """Apply one validated envelope. Returns (action, affected_body_doc_id).

    affected_body_doc_id is the knowledge document id whose chunks must be
    re-evaluated post-commit ('' when none). handshake op: df-upsert-semantics.
    """
    external_id = env["external_id"]
    conn.execute(
        text(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended(:key, 0))"
        ),
        {"key": f"df:{org_id}:{connector_id}:{external_id}"},
    )
    head = conn.execute(
        text(
            """
            SELECT id::text, tombstoned, acl_mode, container_external_id,
                   current_version_id::text
            FROM df_source_records
            WHERE org_id=:org_id AND connector_id=CAST(:cid AS uuid)
              AND external_id=:eid
            FOR UPDATE
            """
        ),
        {"org_id": org_id, "cid": connector_id, "eid": external_id},
    ).mappings().first()

    def _doc_of(body_ref: str) -> str:
        parts = str(body_ref or "").split(":")
        return parts[2] if len(parts) == 3 and parts[0] == "kdv" else ""

    if env["deleted"]:
        if head is None or head["tombstoned"]:
            return "unchanged", ""
        conn.execute(
            text(
                "UPDATE df_source_records SET tombstoned=true, "
                "updated_at=clock_timestamp() "
                "WHERE org_id=:org_id AND id=CAST(:rid AS uuid)"
            ),
            {"org_id": org_id, "rid": head["id"]},
        )
        current = _current_version(conn, org_id, dict(head))
        return "tombstoned", _doc_of((current or {}).get("body_ref", ""))

    if head is None:
        row = conn.execute(
            text(
                """
                INSERT INTO df_source_records (
                  org_id, connector_id, external_id, kind,
                  container_external_id, acl_mode
                ) VALUES (
                  :org_id, CAST(:cid AS uuid), :eid, :kind, :container,
                  :acl_mode
                )
                RETURNING id::text
                """
            ),
            {"org_id": org_id, "cid": connector_id, "eid": external_id,
             "kind": env["kind"], "container": env["container_external_id"],
             "acl_mode": env["acl_mode"]},
        ).mappings().one()
        record_id = row["id"]
        version_id = _insert_version(conn, org_id, record_id, 1, env,
                                     sync_run_id, connector_id)
        _set_head(conn, org_id, record_id, version_id, env)
        _replace_acl(conn, org_id, connector_id, record_id, env["acl_mode"],
                     env["acl"])
        return "created", _doc_of(env.get("body_ref", ""))

    record_id = head["id"]
    current = _current_version(conn, org_id, dict(head)) or {}
    body_changed = current.get("body_checksum") != env["checksum"]
    meta_changed = (
        current.get("title") != env["title"]
        or current.get("mime") != env["mime"]
        or current.get("canonical_url") != env["canonical_url"]
        or current.get("author_external_id") != env["author_external_id"]
        or head["container_external_id"] != env["container_external_id"]
    )
    resurrected = bool(head["tombstoned"])
    acl_before = _acl_signature(conn, org_id, record_id)
    _replace_acl(conn, org_id, connector_id, record_id, env["acl_mode"],
                 env["acl"])
    acl_changed = (_acl_signature(conn, org_id, record_id) != acl_before
                   or head["acl_mode"] != env["acl_mode"])

    if not (body_changed or meta_changed or acl_changed or resurrected):
        return "unchanged", ""

    next_no = 1 + int(conn.execute(
        text(
            "SELECT COALESCE(MAX(version_no), 0) "
            "FROM df_source_record_versions "
            "WHERE org_id=:org_id AND record_id=CAST(:rid AS uuid)"
        ),
        {"org_id": org_id, "rid": record_id},
    ).first()[0])
    if not body_changed and env.get("body_ref", "") == "":
        # Same body, metadata/ACL change: the new version keeps the previous
        # body linkage (checksum dedupe never re-ingests, never suppresses).
        env = {**env, "body_ref": current.get("body_ref", ""),
               "checksum": current.get("body_checksum", env["checksum"])}
    lineage_extra: dict[str, Any] = {}
    if resurrected:
        lineage_extra["resurrected_from_version"] = current.get("version_no")
    if not body_changed:
        lineage_extra["meta_change_of"] = current.get("version_no")
    version_id = _insert_version(conn, org_id, record_id, next_no, env,
                                 sync_run_id, connector_id, lineage_extra)
    _set_head(conn, org_id, record_id, version_id, env, clear_tombstone=True)
    action = ("resurrected" if resurrected
              else "versioned" if body_changed else "meta_updated")
    return action, _doc_of(env.get("body_ref", ""))


def _insert_version(conn, org_id: str, record_id: str, version_no: int,
                    env: dict, sync_run_id: int | None, connector_id: str,
                    lineage_extra: dict | None = None) -> str:
    lineage = {
        "connector_id": connector_id,
        "sync_run_id": sync_run_id,
        "transform": env.get("transform", "df@1"),
        **(lineage_extra or {}),
    }
    row = conn.execute(
        text(
            """
            INSERT INTO df_source_record_versions (
              org_id, record_id, version_no, title, mime, canonical_url,
              author_external_id, body_checksum, body_ref,
              external_updated_at, lineage_json
            ) VALUES (
              :org_id, CAST(:rid AS uuid), :vno, :title, :mime, :url,
              :author, :checksum, :body_ref,
              NULLIF(:ext_updated, '')::timestamptz, CAST(:lineage AS jsonb)
            )
            RETURNING id::text
            """
        ),
        {"org_id": org_id, "rid": record_id, "vno": version_no,
         "title": env["title"], "mime": env["mime"],
         "url": env["canonical_url"], "author": env["author_external_id"],
         "checksum": env["checksum"], "body_ref": env.get("body_ref", ""),
         "ext_updated": env["external_updated_at"],
         "lineage": json.dumps(lineage, separators=(",", ":"),
                               sort_keys=True)},
    ).mappings().one()
    return row["id"]


def _set_head(conn, org_id: str, record_id: str, version_id: str, env: dict,
              *, clear_tombstone: bool = False) -> None:
    conn.execute(
        text(
            f"""
            UPDATE df_source_records
            SET current_version_id=CAST(:vid AS uuid),
                acl_mode=:acl_mode,
                container_external_id=:container,
                {'tombstoned=false,' if clear_tombstone else ''}
                updated_at=clock_timestamp()
            WHERE org_id=:org_id AND id=CAST(:rid AS uuid)
            """
        ),
        {"org_id": org_id, "rid": record_id, "vid": version_id,
         "acl_mode": env["acl_mode"],
         "container": env["container_external_id"]},
    )


def commit_batch(
    org_id: str, connector_id: str, raw_envelopes: list[dict],
    *, new_cursor: dict | None, sync_run_id: int | None = None,
    identities: list[dict] | None = None,
    trusted_email_issuer: bool = False,
    quarantine_payload_ref: Any = None,
) -> dict[str, int]:
    """Apply one ACCEPTED batch + identities + cursor advance ATOMICALLY.

    Invalid envelopes quarantine (durably, inside the same txn) without
    failing the batch; the open-quarantine cap aborts everything with
    BackpressureError BEFORE any change commits; the cursor never advances
    past an unapplied batch. Returns stats + the affected body-doc ids the
    caller must re-index post-commit (stats['affected_docs'])."""
    stats = {"seen": 0, "created": 0, "versioned": 0, "meta_updated": 0,
             "tombstoned": 0, "resurrected": 0, "unchanged": 0,
             "quarantined": 0}
    affected: set[str] = set()
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        open_count = _open_quarantine_count(conn, org_id, connector_id)
        for entry in identities or []:
            iid = _ensure_identity(
                conn, org_id, connector_id,
                str(entry.get("external_id") or ""),
                "group" if entry.get("kind") == "group" else "user",
                display=str(entry.get("display") or ""),
                email_norm=str(entry.get("email") or ""),
            )
            members = entry.get("members")
            if entry.get("kind") == "group" and isinstance(members, list):
                member_ids = [
                    _ensure_identity(conn, org_id, connector_id,
                                     str(m), "user")
                    for m in members[:500]
                ]
                conn.execute(
                    text(
                        "DELETE FROM df_identity_edges "
                        "WHERE org_id=:org_id AND group_id=CAST(:gid AS uuid)"
                    ),
                    {"org_id": org_id, "gid": iid},
                )
                for mid in member_ids:
                    conn.execute(
                        text(
                            """
                            INSERT INTO df_identity_edges
                              (org_id, group_id, member_id)
                            VALUES (:org_id, CAST(:gid AS uuid),
                                    CAST(:mid AS uuid))
                            ON CONFLICT DO NOTHING
                            """
                        ),
                        {"org_id": org_id, "gid": iid, "mid": mid},
                    )
            # AUDITED automatic binding: verified email + trusted issuer ONLY.
            email = str(entry.get("email") or "").strip().lower()
            if (email and bool(entry.get("email_verified"))
                    and trusted_email_issuer
                    and entry.get("kind") != "group"):
                _maybe_bind_verified_email(conn, org_id, iid, email)

        for raw in raw_envelopes:
            stats["seen"] += 1
            clean, errors = envelope_mod.validate_envelope(raw)
            if errors:
                if open_count >= _QUARANTINE_OPEN_CAP:
                    raise BackpressureError(
                        f"open quarantine at cap ({_QUARANTINE_OPEN_CAP})"
                    )
                ref = ""
                if quarantine_payload_ref is not None:
                    try:
                        ref = quarantine_payload_ref(raw)
                    except Exception:  # noqa: BLE001
                        ref = ""
                _quarantine(conn, org_id, connector_id, sync_run_id,
                            str(raw.get("external_id") or "") if
                            isinstance(raw, dict) else "",
                            "; ".join(errors)[:200], ref)
                open_count += 1
                stats["quarantined"] += 1
                continue
            action, doc = _apply_envelope(conn, org_id, connector_id, clean,
                                          sync_run_id)
            stats[action] = stats.get(action, 0) + 1
            if doc:
                affected.add(doc)
        if new_cursor is not None:
            conn.execute(
                text(
                    """
                    INSERT INTO df_connector_cursors
                      (org_id, connector_id, cursor_json, advanced_at)
                    VALUES (:org_id, CAST(:cid AS uuid),
                            CAST(:cursor AS jsonb), clock_timestamp())
                    ON CONFLICT (org_id, connector_id) DO UPDATE SET
                      cursor_json=excluded.cursor_json,
                      advanced_at=clock_timestamp()
                    """
                ),
                {"org_id": org_id, "cid": connector_id,
                 "cursor": json.dumps(new_cursor, separators=(",", ":"),
                                      sort_keys=True)},
            )
    stats["affected_docs"] = sorted(affected)  # type: ignore[assignment]
    return stats


def _maybe_bind_verified_email(conn, org_id: str, identity_id: str,
                               email: str) -> None:
    from .. import store

    try:
        principal = str(store.user_id_for_email(email) or "")
    except Exception:  # noqa: BLE001
        principal = ""
    if not principal:
        return
    conn.execute(
        text(
            """
            INSERT INTO df_principal_bindings
              (org_id, identity_id, principal_ref, method, bound_by)
            VALUES (:org_id, CAST(:iid AS uuid), :principal,
                    'verified_email', 'connector')
            ON CONFLICT (org_id, identity_id) DO NOTHING
            """
        ),
        {"org_id": org_id, "iid": identity_id, "principal": principal[:200]},
    )


def bind_identity(org_id: str, identity_id: str, principal_ref: str,
                  actor: str) -> bool:
    """Explicit admin binding; the audited manual path."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                """
                INSERT INTO df_principal_bindings
                  (org_id, identity_id, principal_ref, method, bound_by)
                VALUES (:org_id, CAST(:iid AS uuid), :principal,
                        'explicit_admin', :actor)
                ON CONFLICT (org_id, identity_id) DO UPDATE SET
                  principal_ref=excluded.principal_ref,
                  method='explicit_admin', bound_by=excluded.bound_by,
                  bound_at=clock_timestamp()
                """
            ),
            {"org_id": org_id, "iid": identity_id,
             "principal": str(principal_ref or "")[:200],
             "actor": str(actor or "")[:64]},
        )
    return bool(result.rowcount)


# ── principal expansion (fail-closed) ───────────────────────────────────────

def principal_identity_closure(
    org_id: str, principal_ref: str
) -> tuple[set[str], bool]:
    """(identity ids incl. group closure, complete). Bindings-table ONLY -
    email similarity never grants access here. Cycles / depth overrun mark
    the expansion incomplete and the affected groups contribute NOTHING."""
    pid = str(principal_ref or "").strip()
    if not pid:
        return set(), True
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                "SELECT identity_id::text FROM df_principal_bindings "
                "WHERE org_id=:org_id AND principal_ref=:pid"
            ),
            {"org_id": org_id, "pid": pid},
        ).fetchall()
        ids = {str(r[0]) for r in rows}
        complete = True
        frontier = set(ids)
        for depth in range(6):
            if not frontier:
                break
            rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT group_id::text FROM df_identity_edges
                    WHERE org_id=:org_id
                      AND member_id = ANY(CAST(:members AS uuid[]))
                    """
                ),
                {"org_id": org_id, "members": sorted(frontier)},
            ).fetchall()
            new_groups = {str(r[0]) for r in rows} - ids
            ids |= new_groups
            frontier = new_groups
        else:
            if frontier:
                # Deeper than the bound (or cyclic churn): FAIL CLOSED; drop
                # everything gathered beyond direct bindings and report.
                complete = False
        if not complete:
            ids = {str(r[0]) for r in conn.execute(
                text(
                    "SELECT identity_id::text FROM df_principal_bindings "
                    "WHERE org_id=:org_id AND principal_ref=:pid"
                ),
                {"org_id": org_id, "pid": pid},
            ).fetchall()}
    return ids, complete


# ── visibility (the one rule) ───────────────────────────────────────────────

def eligible_connector_ids(org_id: str) -> set[str]:
    """Connectors that may contribute records AT ALL: status=active; mirrored
    content additionally requires acl_mirrored authoritative (enforced in the
    record query below)."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                "SELECT id::text FROM df_connectors "
                "WHERE org_id=:org_id AND status='active'"
            ),
            {"org_id": org_id},
        ).fetchall()
    return {str(r[0]) for r in rows}


def visible_heads(
    org_id: str, *, principal_identity_ids: set[str] | None = None,
    connector_ids: list[str] | None = None,
    container_external_ids: list[str] | None = None,
    kinds: list[str] | None = None, limit: int = 500,
) -> list[dict[str, Any]]:
    """Head records visible under the ONE rule. org-subject rows count for
    everyone; identity-subject rows require the caller's closure; mirrored
    records additionally require the owning connector's acl_mirrored=true;
    ineligible connectors contribute nothing; including old records."""
    identity_ids = sorted(principal_identity_ids or set())
    filters, params = [], {
        "org_id": org_id,
        "identity_ids": identity_ids,
        "limit": max(1, min(int(limit), 2000)),
    }
    if connector_ids:
        filters.append("AND r.connector_id = ANY(CAST(:cids AS uuid[]))")
        params["cids"] = [str(c) for c in connector_ids][:50]
    if container_external_ids:
        filters.append("AND r.container_external_id = ANY(:containers)")
        params["containers"] = [str(c) for c in container_external_ids][:50]
    if kinds:
        filters.append("AND r.kind = ANY(:kinds)")
        params["kinds"] = [k for k in kinds
                           if k in envelope_mod.ENVELOPE_KINDS]
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                f"""
                SELECT r.id::text, r.external_id, r.kind,
                       r.container_external_id, r.acl_mode,
                       r.connector_id::text,
                       c.kind AS connector_kind,
                       v.id::text AS version_id, v.title, v.canonical_url,
                       v.body_ref, v.lineage_json,
                       extract(epoch from v.external_updated_at)
                         AS external_updated_at,
                       extract(epoch from v.ingested_at) AS ingested_at
                FROM df_source_records r
                JOIN df_connectors c
                  ON c.org_id=r.org_id AND c.id=r.connector_id
                JOIN df_source_record_versions v
                  ON v.org_id=r.org_id AND v.id=r.current_version_id
                WHERE r.org_id=:org_id AND NOT r.tombstoned
                  AND c.status='active'
                  AND (r.acl_mode <> 'mirrored' OR c.acl_mirrored)
                  AND EXISTS (
                    SELECT 1 FROM df_acl_entries a
                    WHERE a.org_id=r.org_id AND a.record_id=r.id
                      AND (
                        a.subject_kind='org'
                        OR (a.subject_kind='identity'
                            AND a.identity_id =
                                ANY(CAST(:identity_ids AS uuid[])))
                      )
                  )
                  {' '.join(filters)}
                ORDER BY v.ingested_at DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
    return [dict(r) for r in rows]


def restricted_document_ids(org_id: str) -> set[str]:
    """knowledge document ids that must NOT be in the live per-org index:
    tombstoned heads, non-org_default ACL, or an ineligible connector."""
    engine = _engine()
    # ONE snapshot transaction (reverify note): both branches read the same
    # MVCC snapshot, no head can commit between them, and connection churn
    # is halved. Off the live transcript path (callers: index rebuild +
    # dashboard/resolver keyword search), so the extra scans never touch the
    # per-turn budget.
    out: set[str] = set()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        # (a) heads that exist but are not live-index-eligible.
        rows = conn.execute(
            text(
                """
                SELECT v.body_ref
                FROM df_source_records r
                JOIN df_connectors c
                  ON c.org_id=r.org_id AND c.id=r.connector_id
                LEFT JOIN df_source_record_versions v
                  ON v.org_id=r.org_id AND v.id=r.current_version_id
                WHERE r.org_id=:org_id AND v.body_ref <> ''
                  AND (
                    r.tombstoned
                    OR c.status <> 'active'
                    OR r.acl_mode <> 'org_default'
                  )
                """
            ),
            {"org_id": org_id},
        ).fetchall()
        for (body_ref,) in rows:
            parts = str(body_ref or "").split(":")
            if len(parts) == 3 and parts[0] == "kdv":
                out.add(parts[2])
        # (b) FAIL-CLOSED on the publish->commit race: a df:* knowledge
        # document whose body chunks were published by materialize_bodies
        # BEFORE its DF record committed (or whose commit parked/crashed) has
        # NO org_default head yet. Restrict every df:* document not backed by
        # a live org_default eligible head.
        df_docs = conn.execute(
            text(
                """
                SELECT d.id::text
                FROM knowledge_documents d
                JOIN knowledge_sources s
                  ON s.org_id=d.org_id AND s.id=d.source_id
                WHERE d.org_id=:org_id AND s.name LIKE 'df:%'
                  AND d.status='published'
                """
            ),
            {"org_id": org_id},
        ).fetchall()
        if df_docs:
            allowed = conn.execute(
                text(
                    """
                    SELECT split_part(v.body_ref, ':', 3) AS doc_id
                    FROM df_source_records r
                    JOIN df_connectors c
                      ON c.org_id=r.org_id AND c.id=r.connector_id
                    JOIN df_source_record_versions v
                      ON v.org_id=r.org_id AND v.id=r.current_version_id
                    WHERE r.org_id=:org_id AND NOT r.tombstoned
                      AND c.status='active' AND r.acl_mode='org_default'
                      AND v.body_ref LIKE 'kdv:%'
                    """
                ),
                {"org_id": org_id},
            ).fetchall()
            allowed_ids = {str(a[0]) for a in allowed}
            for (doc_id,) in df_docs:
                if str(doc_id) not in allowed_ids:
                    out.add(str(doc_id))
    return out


# ── sync runs / quarantine / cursors / freshness ────────────────────────────

_RUN_LEASE_SECONDS = 600
_RUN_RETRY_SECONDS = (5.0, 30.0, 120.0, 600.0)
_DEAD_LETTER_PARKS = 3


def enqueue_run(org_id: str, connector_id: str,
                kind: str = "incremental") -> int:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                INSERT INTO df_sync_runs (org_id, connector_id, kind,
                                          next_attempt_at)
                VALUES (:org_id, CAST(:cid AS uuid), :kind, clock_timestamp())
                RETURNING id
                """
            ),
            {"org_id": org_id, "cid": connector_id,
             "kind": kind if kind in ("full", "incremental")
             else "incremental"},
        ).first()
    return int(row[0])


def due_orgs(limit: int = 20) -> list[str]:
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM laura_private.due_df_orgs(:limit)"),
            {"limit": max(1, min(int(limit), 100))},
        ).fetchall()
    return [str(r[0]) for r in rows]


def claim_due_runs(org_id: str, limit: int = 2) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                WITH due AS (
                  SELECT id FROM df_sync_runs
                  WHERE org_id=:org_id
                    AND (
                      (status IN ('pending', 'failed')
                       AND next_attempt_at IS NOT NULL
                       AND next_attempt_at <= clock_timestamp())
                      OR (status='running'
                          AND lease_until IS NOT NULL
                          AND lease_until <= clock_timestamp())
                    )
                  ORDER BY
                    CASE WHEN status='running'
                         THEN lease_until ELSE next_attempt_at END, id
                  FOR UPDATE SKIP LOCKED
                  LIMIT :limit
                )
                UPDATE df_sync_runs AS r
                SET status='running',
                    lease_token=gen_random_uuid(),
                    lease_until=clock_timestamp()
                      + (:lease * interval '1 second'),
                    updated_at=clock_timestamp()
                FROM due WHERE r.id=due.id
                RETURNING r.id, r.connector_id::text, r.kind, r.attempts,
                          r.parks, r.lease_token::text
                """
            ),
            {"org_id": org_id, "limit": max(1, min(int(limit), 10)),
             "lease": _RUN_LEASE_SECONDS},
        ).mappings().all()
    return [dict(r) for r in rows]


def finish_run(
    org_id: str, run_id: int, lease_token: str, *, outcome: str,
    stats: dict | None = None, error: str = "", attempts: int = 0,
    parks: int = 0,
) -> bool:
    """outcome: done | retry | park | dead_letter."""
    if outcome == "done":
        assignments = (
            "status='done', attempts=attempts+1, next_attempt_at=NULL, "
            "last_error='', lease_token=NULL, lease_until=NULL, "
            "stats_json=CAST(:stats AS jsonb), updated_at=clock_timestamp()"
        )
    elif outcome == "retry" and int(attempts) < len(_RUN_RETRY_SECONDS):
        assignments = (
            "status='failed', attempts=attempts+1, "
            "next_attempt_at=clock_timestamp() "
            "  + (:retry * interval '1 second'), "
            "last_error=:error, lease_token=NULL, lease_until=NULL, "
            "stats_json=CAST(:stats AS jsonb), updated_at=clock_timestamp()"
        )
    elif outcome == "dead_letter" or int(parks) + 1 >= _DEAD_LETTER_PARKS \
            and outcome == "park":
        assignments = (
            "status='dead_letter', attempts=attempts+1, parks=parks+1, "
            "next_attempt_at=NULL, last_error=:error, lease_token=NULL, "
            "lease_until=NULL, stats_json=CAST(:stats AS jsonb), "
            "updated_at=clock_timestamp()"
        )
    else:  # park (incl. retry ladder exhausted)
        assignments = (
            "status='parked', attempts=attempts+1, parks=parks+1, "
            "next_attempt_at=NULL, last_error=:error, lease_token=NULL, "
            "lease_until=NULL, stats_json=CAST(:stats AS jsonb), "
            "updated_at=clock_timestamp()"
        )
    params = {
        "org_id": org_id, "run_id": int(run_id), "lease_token": lease_token,
        "stats": json.dumps(stats or {}, separators=(",", ":"),
                            sort_keys=True),
        "error": str(error or "")[:200],
        "retry": _RUN_RETRY_SECONDS[min(int(attempts),
                                        len(_RUN_RETRY_SECONDS) - 1)],
    }
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                f"""
                UPDATE df_sync_runs SET {assignments}
                WHERE org_id=:org_id AND id=:run_id AND status='running'
                  AND lease_token=CAST(:lease_token AS uuid)
                """
            ),
            params,
        )
    return bool(result.rowcount)


def retry_run(org_id: str, run_id: int) -> bool:
    """Operator re-drive for parked/dead_letter/failed runs."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                """
                UPDATE df_sync_runs
                SET status='pending', next_attempt_at=clock_timestamp(),
                    last_error='', updated_at=clock_timestamp()
                WHERE org_id=:org_id AND id=:run_id
                  AND status IN ('failed', 'parked', 'dead_letter')
                """
            ),
            {"org_id": org_id, "run_id": int(run_id)},
        )
    return bool(result.rowcount)


def run_rows(org_id: str, connector_id: str = "",
             limit: int = 30) -> list[dict[str, Any]]:
    cid_filter = "AND connector_id=CAST(:cid AS uuid)" if connector_id else ""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                f"""
                SELECT id, connector_id::text, kind, status, attempts, parks,
                       stats_json, last_error,
                       extract(epoch from updated_at) AS updated_at
                FROM df_sync_runs
                WHERE org_id=:org_id {cid_filter}
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            {"org_id": org_id, "cid": connector_id,
             "limit": max(1, min(int(limit), 100))},
        ).mappings().all()
    return [dict(r) for r in rows]


def get_cursor(org_id: str, connector_id: str) -> dict:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                "SELECT cursor_json FROM df_connector_cursors "
                "WHERE org_id=:org_id AND connector_id=CAST(:cid AS uuid)"
            ),
            {"org_id": org_id, "cid": connector_id},
        ).first()
    if row is None:
        return {}
    cursor = row[0]
    if isinstance(cursor, str):
        try:
            cursor = json.loads(cursor)
        except ValueError:
            return {}
    return cursor if isinstance(cursor, dict) else {}


def quarantine_rows(org_id: str, *, state: str = "",
                    limit: int = 50) -> list[dict[str, Any]]:
    state_filter = "AND state=:state" if state else ""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                f"""
                SELECT id::text, connector_id::text, sync_run_id, external_id,
                       reason, payload_ref, state,
                       extract(epoch from created_at) AS created_at,
                       extract(epoch from resolved_at) AS resolved_at,
                       resolved_by
                FROM df_quarantine
                WHERE org_id=:org_id {state_filter}
                ORDER BY created_at DESC LIMIT :limit
                """
            ),
            {"org_id": org_id, "state": state,
             "limit": max(1, min(int(limit), 200))},
        ).mappings().all()
    return [dict(r) for r in rows]


def get_quarantine(org_id: str, qid: str) -> Optional[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                """
                SELECT id::text, connector_id::text, external_id, reason,
                       payload_ref, state
                FROM df_quarantine
                WHERE org_id=:org_id AND id=CAST(:qid AS uuid)
                """
            ),
            {"org_id": org_id, "qid": qid},
        ).mappings().first()
    return dict(row) if row else None


def resolve_quarantine(org_id: str, qid: str, state: str, actor: str) -> bool:
    """Resolve via the SECURITY DEFINER; the runtime has NO direct UPDATE on
    df_quarantine, so resolved_at is stamped server-side and cannot be
    backdated to fast-track a purge."""
    if state not in ("replayed", "discarded"):
        return False
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                "SELECT laura_private.resolve_quarantine("
                "CAST(:org AS uuid), CAST(:qid AS uuid), :state, :actor)"
            ),
            {"org": org_id, "qid": qid, "state": state,
             "actor": str(actor or "")[:64]},
        ).first()
    return bool(row and int(row[0]))


def purge_quarantine(org_id: str, older_than_epoch: float | None = None) -> int:
    """The ONLY purge path; the SECURITY DEFINER function (org verified
    against the transaction context; cutoff clamped server-side)."""
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                "SELECT laura_private.purge_quarantine("
                "CAST(:org AS uuid), "
                "to_timestamp(CAST(:cutoff AS double precision)))"
            ),
            {"org": org_id, "cutoff": older_than_epoch},
        ).first()
    return int(row[0])


def purge_record_versions(org_id: str,
                          older_than_epoch: float | None = None) -> int:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        row = conn.execute(
            text(
                "SELECT laura_private.purge_record_versions("
                "CAST(:org AS uuid), "
                "to_timestamp(CAST(:cutoff AS double precision)))"
            ),
            {"org": org_id, "cutoff": older_than_epoch},
        ).first()
    return int(row[0])


def pending_purge_audits(org_id: str) -> list[dict[str, Any]]:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT id, purged_count, payload_refs_pending,
                       extract(epoch from created_at) AS created_at
                FROM df_purge_audit
                WHERE org_id=:org_id AND completed_at IS NULL
                ORDER BY id LIMIT 20
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
    out = []
    for r in rows:
        item = dict(r)
        refs = item.get("payload_refs_pending")
        if isinstance(refs, str):
            try:
                refs = json.loads(refs)
            except ValueError:
                refs = []
        item["payload_refs_pending"] = refs if isinstance(refs, list) else []
        out.append(item)
    return out


def complete_purge_audit(org_id: str, audit_id: int) -> bool:
    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        result = conn.execute(
            text(
                """
                UPDATE df_purge_audit
                SET completed_at=clock_timestamp()
                WHERE org_id=:org_id AND id=:aid AND completed_at IS NULL
                """
            ),
            {"org_id": org_id, "aid": int(audit_id)},
        )
    return bool(result.rowcount)


def freshness(org_id: str) -> dict[str, Any]:
    import time as _time

    engine = _engine()
    with engine.begin() as conn:
        _set_org(conn, org_id)
        rows = conn.execute(
            text(
                """
                SELECT c.id::text, c.kind, c.name, c.status,
                       (SELECT extract(epoch from max(r.updated_at))
                          FROM df_sync_runs r
                         WHERE r.org_id=c.org_id AND r.connector_id=c.id
                           AND r.status='done') AS last_success_at
                FROM df_connectors c
                WHERE c.org_id=:org_id AND c.status <> 'revoked'
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
        bounds = conn.execute(
            text(
                """
                SELECT extract(epoch from min(v.ingested_at)),
                       extract(epoch from max(v.ingested_at)),
                       extract(epoch from
                         max(r.updated_at) FILTER (WHERE r.tombstoned))
                FROM df_source_records r
                LEFT JOIN df_source_record_versions v
                  ON v.org_id=r.org_id AND v.id=r.current_version_id
                WHERE r.org_id=:org_id
                """
            ),
            {"org_id": org_id},
        ).first()
    now = _time.time()
    stale = [
        {"connector_id": r["id"], "kind": r["kind"], "name": r["name"],
         "status": r["status"], "last_success_at": r["last_success_at"]}
        for r in rows
        if r["status"] != "active"
        or r["last_success_at"] is None
        or (now - float(r["last_success_at"])) > 24 * 3600
    ]
    newest_tombstone = float(bounds[2]) if bounds and bounds[2] else None
    return {
        "oldest_ingested_at": float(bounds[0]) if bounds and bounds[0] else None,
        "newest_ingested_at": float(bounds[1]) if bounds and bounds[1] else None,
        "stale_connectors": stale,
        "tombstone_lag_seconds": (
            min(60.0, now - newest_tombstone) if newest_tombstone else 0.0
        ),
    }
