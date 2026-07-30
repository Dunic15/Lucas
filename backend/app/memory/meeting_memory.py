"""Meeting Memory Slice 1: deposit at finalize, past-week digest at join.

Two entry points, both best-effort and both OFF the transcript→token path:

- ``deposit(session, artifact)`` — called once at finalize (threadpool, beside
  ``ledger.record_meeting``): folds the meeting's DISTILLED artifact fields
  into ``memory_meetings``/``memory_attendees``/``memory_entities``. The
  artifact's ``transcript`` and each action's ``evidence`` (a verbatim
  transcript excerpt) are PII and never written here.
- ``week_brief(org_id, avatar_id)`` — called once at session start (threadpool,
  inside the existing best-effort gather): returns the cached past-7-days
  digest, regenerating it with ONE fast-model call when the TTL lapsed. Stub
  brain or any model failure degrades to a deterministic dated bullet list, so
  the join can never be delayed or broken by the model.

Entity dedupe is the ``UNIQUE (org_id, kind, key)`` constraint: person key is
the lowercased email when known, else ``dn:`` + normalized display name —
writers upsert and physically cannot create a second node for the same key.
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text

from .. import control_plane
from ..actions import ledger
from ..config import settings

# Cap on the distilled rows fed to the digest model — input tokens are money
# and tail latency; 25 meetings × ~240 chars ≈ the whole week for our ICP.
_DIGEST_INPUT_CHARS = 6000
_WINDOW_MEETINGS = 25
# Per-field truncation for stored distilled lines (mirrors ledger's 200-char
# item cap — memory rows must stay brief-sized, not document-sized).
_FIELD_CHARS = 240
_SUMMARY_CHARS = 2000

WEEK_BRIEF_SYSTEM = (
    "You compress one company's past week of meetings into working memory for "
    "an AI meeting assistant that is about to join a new meeting. Write at "
    "most 150 words as short dated lines (format: 'MM-DD — ...'), most recent "
    "first. Decisions and still-open actions first, then themes. Name people "
    "and owners when given. Plain text only, no preamble, no headers."
)


def enabled() -> bool:
    return bool(settings.meeting_memory_enabled) and control_plane.enabled()


def _engine():
    return control_plane._get_engine()


def _set_org(conn, org_id: str) -> None:
    control_plane._set_org(conn, org_id)


def _norm_dn(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().casefold())


def _clip(value: Any, limit: int = _FIELD_CHARS) -> str:
    return str(value or "").strip()[:limit]


def _distill_actions(actions: Any) -> list[dict[str, str]]:
    """Keep the distilled action fields; DROP ``evidence`` — it is a verbatim
    transcript excerpt (PII) and must never reach a memory row."""
    out: list[dict[str, str]] = []
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        out.append(
            {
                "item": _clip(a.get("item")),
                "owner": _clip(a.get("owner"), 80),
                "deadline": _clip(a.get("deadline"), 80),
                "action_id": _clip(a.get("action_id"), 32),
                "gap_type": _clip(a.get("gap_type"), 32),
            }
        )
    return out


def _attendees_of(session: Any, artifact: dict) -> list[dict[str, Any]]:
    """Merge the live roster (display names, no emails — Recall sends none)
    with the artifact's participation lines (who actually spoke)."""
    spoke = {
        _norm_dn(str(p.get("name") or ""))
        for p in (artifact.get("participation") or [])
        if isinstance(p, dict)
    }
    seen: dict[str, dict[str, Any]] = {}
    for identity in (getattr(session, "participants", None) or {}).values():
        if str(identity.get("kind") or "human") != "human":
            continue
        name = str(identity.get("name") or "").strip()
        key = _norm_dn(name)
        if not key or key in seen:
            continue
        email = str(identity.get("email") or "").strip().lower()
        seen[key] = {
            "display": name[:120],
            "email": email,
            "resolution": "email" if email else "display_name",
            "spoke": key in spoke,
        }
    # Speakers known only from the transcript labels (roster gap) still count.
    for p in artifact.get("participation") or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        key = _norm_dn(name)
        if not key or key in seen:
            continue
        seen[key] = {
            "display": name[:120],
            "email": "",
            "resolution": "display_name",
            "spoke": True,
        }
    return list(seen.values())


def deposit(session: Any, artifact: dict) -> bool:
    """Fold one finished meeting into the memory tables. Never raises — the
    finalize path's cleanup (meter stop) must not depend on memory."""
    try:
        if not enabled():
            return False
        org_id = str(getattr(session, "org_id", "") or "")
        bot_id = str(getattr(session, "bot_id", "") or "")
        if not bot_id or not control_plane.is_durable_org(org_id):
            return False
        meeting_url = str(
            artifact.get("meeting_url")
            or getattr(session, "meeting_url", "")
            or ""
        )
        duration = int(artifact.get("duration_seconds") or 0)
        row = {
            "org_id": org_id,
            "bot_id": bot_id,
            "meeting_key": ledger.meeting_key(meeting_url) if meeting_url else "",
            "avatar_id": _clip(
                artifact.get("avatar_id") or getattr(session, "avatar_id", ""), 80
            ),
            "meeting_type": _clip(artifact.get("meeting_type"), 80),
            "duration_seconds": duration,
            "readiness_score": int(artifact.get("readiness_score") or 0),
            "summary": _clip(artifact.get("summary"), _SUMMARY_CHARS),
            "decisions_json": json.dumps(
                [_clip(d) for d in (artifact.get("decisions") or []) if str(d).strip()]
            ),
            "actions_json": json.dumps(_distill_actions(artifact.get("actions"))),
            "missing_steps_json": json.dumps(
                [_clip(s) for s in (artifact.get("missing_steps") or []) if str(s).strip()]
            ),
        }
        attendees = _attendees_of(session, artifact)
        with _engine().begin() as conn:
            _set_org(conn, org_id)
            meeting_id = conn.execute(
                text(
                    """
                    INSERT INTO memory_meetings (
                      org_id, bot_id, meeting_key, avatar_id, meeting_type,
                      started_at, ended_at, duration_seconds, readiness_score,
                      summary, decisions_json, actions_json, missing_steps_json
                    ) VALUES (
                      CAST(:org_id AS uuid), :bot_id, :meeting_key, :avatar_id,
                      :meeting_type,
                      clock_timestamp() - (:duration_seconds * interval '1 second'),
                      clock_timestamp(), :duration_seconds, :readiness_score,
                      :summary, :decisions_json, :actions_json,
                      :missing_steps_json
                    )
                    ON CONFLICT (org_id, bot_id) DO UPDATE SET
                      summary = EXCLUDED.summary,
                      decisions_json = EXCLUDED.decisions_json,
                      actions_json = EXCLUDED.actions_json,
                      missing_steps_json = EXCLUDED.missing_steps_json,
                      readiness_score = EXCLUDED.readiness_score,
                      duration_seconds = EXCLUDED.duration_seconds,
                      ended_at = clock_timestamp()
                    RETURNING id
                    """
                ),
                row,
            ).scalar_one()
            for att in attendees:
                key = att["email"] or ("dn:" + _norm_dn(att["display"]))
                entity_id = conn.execute(
                    text(
                        """
                        INSERT INTO memory_entities (
                          org_id, kind, key, display, email
                        ) VALUES (
                          CAST(:org_id AS uuid), 'person', :key, :display,
                          :email
                        )
                        ON CONFLICT (org_id, kind, key) DO UPDATE SET
                          display = EXCLUDED.display,
                          last_seen_at = clock_timestamp(),
                          mention_count = memory_entities.mention_count + 1
                        RETURNING id
                        """
                    ),
                    {
                        "org_id": org_id,
                        "key": key,
                        "display": att["display"],
                        "email": att["email"],
                    },
                ).scalar_one()
                conn.execute(
                    text(
                        """
                        INSERT INTO memory_attendees (
                          org_id, meeting_id, entity_id, display, email,
                          resolution, spoke
                        ) VALUES (
                          CAST(:org_id AS uuid), :meeting_id, :entity_id,
                          :display, :email, :resolution, :spoke
                        )
                        ON CONFLICT (org_id, meeting_id, entity_id)
                        DO UPDATE SET spoke = EXCLUDED.spoke
                        """
                    ),
                    {
                        "org_id": org_id,
                        "meeting_id": meeting_id,
                        "entity_id": entity_id,
                        "display": att["display"],
                        "email": att["email"],
                        "resolution": att["resolution"],
                        "spoke": bool(att["spoke"]),
                    },
                )
        return True
    except Exception:
        return False


# ── past-week digest ────────────────────────────────────────────────────────

def _window_rows(conn, org_id: str, avatar_id: str) -> list[dict]:
    sql = """
        SELECT bot_id, meeting_key, meeting_type, summary, decisions_json,
               actions_json, ended_at::text AS ended_at,
               to_char(ended_at, 'MM-DD') AS day
        FROM memory_meetings
        WHERE org_id = CAST(:org_id AS uuid)
          AND ended_at > clock_timestamp() - interval '7 days'
    """
    params: dict[str, Any] = {"org_id": org_id, "limit": _WINDOW_MEETINGS}
    if avatar_id:
        sql += " AND avatar_id = :avatar_id"
        params["avatar_id"] = avatar_id
    sql += " ORDER BY ended_at DESC LIMIT :limit"
    return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


def _rows_as_input(rows: list[dict]) -> str:
    lines: list[str] = []
    for r in rows:
        decisions = [d for d in json.loads(r["decisions_json"] or "[]") if d][:3]
        actions = json.loads(r["actions_json"] or "[]")[:3]
        parts = [f"{r['day']} [{r['meeting_type'] or 'meeting'}] {r['summary'][:200]}"]
        if decisions:
            parts.append("decided: " + "; ".join(d[:100] for d in decisions))
        if actions:
            parts.append(
                "actions: "
                + "; ".join(
                    f"{a.get('item', '')[:80]}"
                    + (f" ({a.get('owner')})" if a.get("owner") else "")
                    for a in actions
                )
            )
        lines.append(" | ".join(parts))
    return "\n".join(lines)[:_DIGEST_INPUT_CHARS]


def _fallback_digest(rows: list[dict], max_chars: int) -> str:
    """Deterministic no-model digest — the stub/key-free/model-down path still
    shows real accumulated memory, just plainer."""
    lines: list[str] = []
    for r in rows:
        decisions = [d for d in json.loads(r["decisions_json"] or "[]") if d]
        line = f"- {r['day']} [{r['meeting_type'] or 'meeting'}]: {r['summary'][:140]}"
        if decisions:
            line += f" Decided: {decisions[0][:100]}"
        lines.append(line)
    return "\n".join(lines)[:max_chars]


def cached_digest(org_id: str, avatar_id: str = "") -> str:
    """Read-only digest fetch for the LIVE path (post-restart brief rebuild):
    returns whatever digest row exists — even TTL-stale — and NEVER makes a
    model call or writes. A slightly stale week block beats a model call on
    the transcript→token path. Returns "" when disabled/absent; never raises."""
    try:
        if not enabled() or not control_plane.is_durable_org(str(org_id or "")):
            return ""
        scope = "avatar" if avatar_id else "org"
        with _engine().begin() as conn:
            _set_org(conn, str(org_id))
            row = conn.execute(
                text(
                    """
                    SELECT text FROM memory_digests
                    WHERE org_id = CAST(:org_id AS uuid)
                      AND scope = :scope AND avatar_id = :avatar_id
                    """
                ),
                {
                    "org_id": str(org_id),
                    "scope": scope,
                    "avatar_id": str(avatar_id or ""),
                },
            ).scalar()
        return str(row or "")[: int(settings.meeting_memory_digest_max_chars)]
    except Exception:
        return ""


def week_brief(org_id: str, avatar_id: str = "") -> str:
    """The past-7-days working memory block, cached per (org, avatar) with a
    TTL so it costs ONE fast-model call per window, not one per join. Returns
    "" when disabled/empty. Never raises."""
    try:
        if not enabled() or not control_plane.is_durable_org(str(org_id or "")):
            return ""
        org_id = str(org_id)
        avatar_id = str(avatar_id or "")
        scope = "avatar" if avatar_id else "org"
        max_chars = int(settings.meeting_memory_digest_max_chars)
        with _engine().begin() as conn:
            _set_org(conn, org_id)
            cached = conn.execute(
                text(
                    """
                    SELECT text FROM memory_digests
                    WHERE org_id = CAST(:org_id AS uuid)
                      AND scope = :scope AND avatar_id = :avatar_id
                      AND generated_at > clock_timestamp()
                          - (:ttl * interval '1 second')
                    """
                ),
                {
                    "org_id": org_id,
                    "scope": scope,
                    "avatar_id": avatar_id,
                    "ttl": int(settings.meeting_memory_digest_ttl_seconds),
                },
            ).scalar()
            if cached:
                return str(cached)[:max_chars]
            rows = _window_rows(conn, org_id, avatar_id)
        if not rows:
            return ""

        digest = ""
        from ..brain import engine as brain_engine  # lazy: heavy module

        if not brain_engine._is_stub():
            try:
                from ..brain import llm

                digest = llm.complete(
                    WEEK_BRIEF_SYSTEM,
                    _rows_as_input(rows),
                    max_tokens=400,
                    model=settings.brain_model_fast,
                ).strip()
            except Exception:
                digest = ""
        if not digest:
            digest = _fallback_digest(rows, max_chars)
        digest = digest[:max_chars]
        if not digest:
            return ""

        with _engine().begin() as conn:
            _set_org(conn, org_id)
            conn.execute(
                text(
                    """
                    INSERT INTO memory_digests (
                      org_id, scope, avatar_id, window_start, window_end,
                      text, source_meeting_ids_json, model, generated_at
                    ) VALUES (
                      CAST(:org_id AS uuid), :scope, :avatar_id,
                      clock_timestamp() - interval '7 days', clock_timestamp(),
                      :text, :sources, :model, clock_timestamp()
                    )
                    ON CONFLICT (org_id, scope, avatar_id) DO UPDATE SET
                      text = EXCLUDED.text,
                      window_start = EXCLUDED.window_start,
                      window_end = EXCLUDED.window_end,
                      source_meeting_ids_json = EXCLUDED.source_meeting_ids_json,
                      model = EXCLUDED.model,
                      generated_at = clock_timestamp()
                    """
                ),
                {
                    "org_id": org_id,
                    "scope": scope,
                    "avatar_id": avatar_id,
                    "text": digest,
                    "sources": json.dumps([r["bot_id"] for r in rows]),
                    "model": settings.brain_model_fast,
                },
            )
        return digest
    except Exception:
        return ""
