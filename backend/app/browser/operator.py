"""The canonical BrowserOperator (B0) — the one place provider is touched.

Owns: the state machine (creating → ready → presenting → closing → closed,
terminal failed/expired/revoked), tenancy binding, command sequencing +
idempotency, deterministic policy classification, presentation-token
issuance/exchange, and the routing of WRITE commands onto the existing Action
Control Plane (route='browser') — never a second execution system.

Tenancy comes ONLY from the authenticated caller the router resolves; nothing
here reads org/principal from a provider ref or a client body. Ownership,
state, resolved-avatar permission and tool allowance are re-checked at
command time AND again at approved-execution time.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from . import contracts, dal, policy, tokens
from .provider import (
    ProviderError,
    ProviderTimeout,
    ProviderUnconfigured,
    get_provider,
)

# States in which read/observe commands are allowed.
_COMMANDABLE = ("ready", "presenting")


class OwnershipError(RuntimeError):
    pass


def _config():
    from ..config import settings

    return settings


def _resolved_avatar(org_id: str, avatar_key: str):
    from .. import avatar_resolver

    return avatar_resolver.resolve(org_id, avatar_key)


def _browser_allowed(org_id: str, avatar_key: str) -> bool:
    """Execution-time tool-allowance check: the resolved avatar must permit
    the 'browser' capability family. Flag-off / no overlay ⇒ allowed
    (today's behavior); an overlay can only narrow."""
    from .. import avatar_resolver

    return avatar_resolver.family_allowed(org_id, avatar_key, "browser")


# ── lifecycle ───────────────────────────────────────────────────────────────

def create_session(
    org_id: str, *, principal: str, avatar_key: str,
    meeting_ref: str = "", provider_name: str = "", metadata: dict | None = None,
) -> dict[str, Any]:
    """Create + bind a session. Raises ProviderUnconfigured if the chosen
    provider has no config (the caller maps to 503). Tenancy is the caller's
    authenticated (org, principal). ``metadata`` is bounded, non-authoritative
    demo-run labelling only."""
    from .provider import default_provider_name

    if not _browser_allowed(org_id, avatar_key):
        raise OwnershipError("browser tool not allowed for this avatar")
    name = (provider_name or default_provider_name()).strip()
    provider = get_provider(name)
    resolved = _resolved_avatar(org_id, avatar_key)
    version = int(getattr(resolved, "overlay_version", 0) or 0)
    ttl = int(_config().browser_default_timeout_seconds)
    prov = provider.create(ttl_seconds=ttl)  # may raise ProviderUnconfigured
    row = dal.create_session(
        org_id, principal=principal, avatar_key=avatar_key,
        avatar_version=version, meeting_ref=meeting_ref, provider=name,
        provider_ref=prov.provider_ref, ttl_seconds=ttl,
        metadata=contracts.clean_metadata(metadata),
    )
    return dal.public_view(row)


def set_metadata(org_id: str, session_id: str, metadata: dict,
                 *, principal: str = "") -> dict:
    """Update the bounded, NON-authoritative demo-run metadata. Never touches
    state, policy or the provider (the demo integrator's label channel)."""
    row = dal.get_session_internal(org_id, session_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    if not _owns(row, principal):
        return {"ok": False, "reason": "not_owner"}
    dal.set_metadata(org_id, session_id, contracts.clean_metadata(metadata))
    return {"ok": True, "metadata": contracts.clean_metadata(metadata)}


def _owns(row: dict, principal: str) -> bool:
    """A caller with a principal may only touch its OWN session; a machine
    caller (principal='') carries org authority and passes."""
    return not (principal and row.get("principal")
                and row["principal"] != principal)


def get_session(org_id: str, session_id: str,
                *, principal: str = "") -> Optional[dict]:
    row = dal.get_session_internal(org_id, session_id)
    if row is None:
        return None
    if not _owns(row, principal):
        # Ownership: a different principal in the same org cannot see the
        # session (indistinguishable from not-found).
        return None
    return dal.public_view(row)


def close_session(org_id: str, session_id: str, *, principal: str = "") -> dict:
    """Idempotent close. Terminal states return success without touching the
    provider again. Ownership is re-checked (a same-org non-owner cannot close
    another principal's live session)."""
    row = dal.get_session_internal(org_id, session_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    if not _owns(row, principal):
        return {"ok": False, "reason": "not_owner"}
    if row["state"] in ("closed", "revoked", "expired", "failed"):
        return {"ok": True, "state": row["state"], "idempotent": True}
    dal.set_state(org_id, session_id, "closing")
    _close_provider(row)
    dal.set_state(org_id, session_id, "closed")
    return {"ok": True, "state": "closed"}


def revoke_session(org_id: str, session_id: str, *, principal: str = "") -> dict:
    """Idempotent revoke — a security stop. Provider released, tokens killed.
    Ownership re-checked."""
    row = dal.get_session_internal(org_id, session_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    if not _owns(row, principal):
        return {"ok": False, "reason": "not_owner"}
    if row["state"] in ("closed", "revoked", "expired", "failed"):
        return {"ok": True, "state": row["state"], "idempotent": True}
    _close_provider(row)
    dal.set_state(org_id, session_id, "revoked")
    return {"ok": True, "state": "revoked"}


def _close_provider(row: dict) -> None:
    try:
        get_provider(row["provider"]).close(row["provider_ref"])
    except Exception:  # noqa: BLE001 — provider release is best-effort
        pass


# ── presentation ────────────────────────────────────────────────────────────

def present(org_id: str, session_id: str, *, principal: str = "") -> dict:
    """Move ready→presenting and mint a fresh opaque presentation token. The
    token value is returned ONCE; only its hash is stored. Ownership is
    re-checked, and the ready→presenting move is an ATOMIC guarded transition
    so a concurrent close/revoke can never be resurrected into presenting."""
    row = dal.get_session_internal(org_id, session_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    if not _owns(row, principal):
        return {"ok": False, "reason": "not_owner"}
    if row["state"] not in _COMMANDABLE:
        return {"ok": False, "reason": f"not_presentable:{row['state']}"}
    if row["state"] == "ready":
        # Atomic: only ready→presenting wins; if a concurrent close moved it
        # to closing/closed/revoked, this fails and we refuse (no resurrection).
        if not dal.transition(org_id, session_id, "presenting", ("ready",)):
            fresh = dal.get_session_internal(org_id, session_id)
            state = fresh["state"] if fresh else "closed"
            return {"ok": False, "reason": f"not_presentable:{state}"}
    value, token_hash = tokens.mint()
    ttl = int(_config().browser_presentation_token_ttl_seconds)
    created = dal.create_token(org_id, session_id, token_hash, ttl)
    return {"ok": True, "state": "presenting",
            "presentation_token": value,  # shown once, never logged/stored raw
            "expires_at": dal._num(created["expires_at"])}


def exchange_token(org_id: str, token_value: str) -> dict:
    """Server-side exchange of an opaque token for the CURRENT read-only
    viewer payload. Replay-checked: revoked/expired/non-live all deny."""
    if not tokens.looks_like_token(token_value):
        return {"ok": False, "reason": "malformed"}
    verdict = dal.redeem_token(org_id, tokens.hash_token(token_value))
    if verdict is None or not verdict.get("valid"):
        return {"ok": False, "reason": (verdict or {}).get("reason", "invalid")}
    row = dal.get_session_internal(org_id, verdict["session_id"])
    if row is None or row["state"] not in _COMMANDABLE:
        return {"ok": False, "reason": "session_not_live"}
    try:
        viewer = get_provider(row["provider"]).viewer(row["provider_ref"])
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "viewer_unavailable"}
    # Belt+braces: the payload is read-only and secret-free by construction;
    # redact any stray secret shape and never include a provider ref.
    return {"ok": True, "viewer": _safe_viewer(viewer)}


def _safe_viewer(viewer: dict) -> dict:
    out = {}
    for key, value in (viewer or {}).items():
        if key in ("provider_ref", "connect_url"):
            continue  # never expose provider internals
        out[key] = policy.redact(value) if isinstance(value, str) else value
    out["read_only"] = True
    return out


# ── commands ────────────────────────────────────────────────────────────────

_RECOVERABLE_FAILURES = ("execution_unknown", "provider_unconfigured",
                         "write_rejected_read_only", "verification_failed")


def issue_command(
    org_id: str, session_id: str, *, verb: str, principal: str = "",
    command_id: str = "", element_id: str = "", url: str = "",
    text: str = "", direction: str = "down",
    verify: bool = False, expected: dict | None = None,
) -> dict:
    """Execute one read-only command or route a WRITE to the approval plane,
    returning the stable CommandResult contract (contracts.command_result).

    Idempotent on (org, session, command_id). Re-checks ownership, session
    state, resolved-avatar tool allowance, and — before any click/type —
    classifies against the LIVE observation via the deterministic policy
    engine. When ``verify`` is set, the operator RE-OBSERVES after executing
    and compares against ``expected`` (the planner never declares its own
    success). Bounded and secret-free."""
    if verb not in policy.READ_ONLY_VERBS:
        return contracts.command_result(
            accepted=False, command_sequence=0, page_version=0,
            classification="", failure_category="unknown_verb",
            replanning_permitted=False,
            extra={"ok": False, "reason": f"unknown_verb:{verb}"})
    command_id = str(command_id or "")[:200]  # cap: it is a PK column

    # Load + OWNERSHIP check BEFORE anything reads or writes command state, so a
    # same-org non-owner can neither read a victim's recorded observation via
    # replay nor poison a claim row (both are the same fix-4 ordering rule).
    row = dal.get_session_internal(org_id, session_id)
    if row is None:
        return _reject("not_found", 0, 0, replanning=False)
    if not _owns(row, principal):
        return _reject("not_owner", int(row["last_command_seq"]),
                       int(row["page_version"]), replanning=False)

    # A previously-completed command replays its recorded result — idempotent
    # even if the session has since changed state (so a retry after close still
    # returns the recorded result rather than an invalid_state error).
    replay = dal.recorded_command(org_id, session_id, command_id)
    if replay is not None and replay.get("_done"):
        return {**{k: v for k, v in replay.items() if k != "_done"},
                "idempotent_replay": True}

    if row["state"] not in _COMMANDABLE:
        return _reject("invalid_state", int(row["last_command_seq"]),
                       int(row["page_version"]), replanning=False,
                       reason=f"invalid_state:{row['state']}")
    if not _browser_allowed(org_id, row["avatar_key"]):
        return _reject("browser_tool_not_allowed", int(row["last_command_seq"]),
                       int(row["page_version"]), replanning=False)

    # Atomically claim the command_id: the single winner executes; a
    # concurrent owner loses the claim and returns without side effects.
    if command_id and not dal.claim_command(org_id, session_id, command_id,
                                            verb):
        return {"ok": True, "idempotent_replay": True,
                "reason": "in_flight_or_done"}

    provider = get_provider(row["provider"])
    ref = row["provider_ref"]
    try:
        # Always observe the live page first, so classification and receipts
        # reflect real DOM (spec §8: plan -> DOM verification -> classify).
        current = policy.sanitize_observation(provider.observe(ref))
        result = _dispatch(org_id, row, provider, ref, verb, element_id,
                           url, text, direction, current)
    except ProviderTimeout:
        # The action MAY have landed — honest unknown, never blind-retry.
        result = {"ok": False, "reason": "execution_unknown",
                  "note": "provider timeout; verify by observing"}
    except ProviderUnconfigured as exc:
        result = {"ok": False, "reason": "provider_unconfigured",
                  "detail": str(exc)[:120]}
    except ProviderError:
        # Provider/browser error → the session is no longer trustworthy.
        dal.set_state(org_id, session_id, "failed")
        result = {"ok": False, "reason": "provider_error", "state": "failed"}

    seq = dal.bump_seq(org_id, session_id)
    observation = result.get("observation")
    page_version = int(row["page_version"])
    if isinstance(observation, dict):
        page_version, _ = dal.sync_page_version(
            org_id, session_id, _observation_fingerprint(observation))
        observation = contracts.build_observation(
            session_id=session_id, command_sequence=seq,
            page_version=page_version, sanitized=observation,
            timestamp=_now())
        result["observation"] = observation

    # Visual verification (the operator decides success, never the planner).
    verification = ""
    if verify and result.get("ok") and isinstance(observation, dict):
        verification = contracts.verify_expectation(expected or {}, observation)
        if verification == contracts.NOT_VERIFIED:
            result["ok"] = False
            result["reason"] = "verification_failed"

    unified = _to_command_result(result, seq, page_version, verification)
    dal.finalize_command(org_id, session_id, command_id, seq,
                         {**unified, "_done": True})
    return unified


def _reject(failure: str, seq: int, page_version: int, *,
            replanning: bool, reason: str = "") -> dict:
    return contracts.command_result(
        accepted=False, command_sequence=seq, page_version=page_version,
        classification="", failure_category=failure,
        replanning_permitted=replanning,
        extra={"ok": False, "reason": reason or failure, "seq": seq})


def _to_command_result(result: dict, seq: int, page_version: int,
                       verification: str) -> dict:
    """Merge the internal result into the stable CommandResult contract while
    keeping the legacy keys (ok/reason/class/observation/proposed) existing
    callers and tests rely on."""
    reason = str(result.get("reason") or "")
    # approval_required is PENDING, not a failure — action_id carries the wait.
    if reason == "approval_required":
        failure, replanning = "", False
    elif result.get("ok"):
        failure, replanning = "", True
    else:
        failure = reason.split(":", 1)[0] if reason else "error"
        replanning = failure in _RECOVERABLE_FAILURES
    return contracts.command_result(
        accepted=bool(result.get("ok")),
        command_sequence=seq, page_version=page_version,
        classification=str(result.get("class") or ""),
        observation=result.get("observation"),
        observation_ref=(result.get("observation") or {}).get(
            "screenshot_ref", "") if isinstance(
                result.get("observation"), dict) else "",
        action_id=str(result.get("action_id") or ""),
        failure_category=failure,
        replanning_permitted=replanning,
        verification=verification,
        extra={**{k: v for k, v in result.items()
                  if k in ("ok", "reason", "class", "observation", "proposed",
                           "policy", "note", "detail", "state")},
               "seq": seq},
    )


def _now() -> float:
    import time

    return time.time()


def _observation_fingerprint(observation: dict) -> str:
    import hashlib
    import json as _json

    basis = _json.dumps({
        "url": observation.get("url"),
        "title": observation.get("title"),
        "elements": sorted(str(e.get("id"))
                           for e in observation.get("elements") or []),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(basis.encode()).hexdigest()


def _dispatch(org_id, row, provider, ref, verb, element_id, url, text,
              direction, current) -> dict:
    classification = policy.classify(
        verb, current, element_id=element_id, text=text
    )
    cls = classification["class"]
    if cls == "blocked":
        return {"ok": False, "reason": "blocked",
                "policy": classification["reason"]}
    if cls == "guarded":
        return _route_write_to_action(org_id, row, verb, element_id, text,
                                      current, classification)

    # auto (read-only) — execute against the provider.
    if verb == "observe":
        obs = current
    elif verb == "navigate":
        obs = policy.sanitize_observation(provider.navigate(ref, url))
    elif verb == "scroll":
        obs = policy.sanitize_observation(
            provider.scroll(ref, direction))
    elif verb == "click":
        obs = policy.sanitize_observation(provider.click(ref, element_id))
    elif verb == "type":
        obs = policy.sanitize_observation(
            provider.type_text(ref, element_id, text))
    else:
        return {"ok": False, "reason": f"unknown_verb:{verb}"}
    return {"ok": True, "class": "auto", "observation": obs}


def _route_write_to_action(org_id, row, verb, element_id, text, current,
                           classification) -> dict:
    """A guarded WRITE never executes inline. B0 posture: with writes
    disabled (default) it is REJECTED and surfaced; when
    BROWSER_ALLOW_WRITES is on it becomes a canonical Action
    (route='browser') requiring approval — the SAME plane every other action
    uses, no second system."""
    element = policy._element(current, element_id) or {}
    projection = policy.safe_param_projection(verb, element, text)
    # Bind the approval to the PAGE STATE with a fingerprint reproducible from
    # any fresh observation (url+title+element-ids), so execute_approved_step
    # can re-verify it. The target element id is bound too.
    fingerprint = _observation_fingerprint(current)
    if not _config().browser_allow_writes:
        return {"ok": False, "reason": "write_rejected_read_only",
                "class": "guarded",
                "proposed": projection,
                "policy": classification["reason"]}
    action_id = _mint_browser_action(org_id, row, projection, fingerprint,
                                     element_id)
    return {"ok": False, "reason": "approval_required", "class": "guarded",
            "action_id": action_id, "proposed": projection}


def _mint_browser_action(org_id, row, projection, fingerprint,
                         element_id="") -> str:
    """Create a canonical queued action, route='browser', carrying the safe
    param projection and the approval binding — reusing the M0 plane."""
    from .. import ledger

    action_id = ledger.new_action_id()
    typed = {"type": f"browser.{projection['action']}",
             "args": {"target": projection["target"],
                      "text": projection.get("text", "")}}
    ledger.set_action_status(action_id, "proposed",
                             f"browser: {projection['action']}", org_id=org_id)
    _index_browser_action(org_id, row, action_id, typed, fingerprint,
                          element_id)
    return action_id


def _index_browser_action(org_id, row, action_id, typed, fingerprint,
                          element_id="") -> None:
    """Index the browser action into queued_actions with route='browser',
    the binding in permission_json, and this session as the source — so the
    existing approve door + claim + receipt operate on it unchanged."""
    from sqlalchemy import text as sql

    from .. import control_plane

    engine = control_plane._get_engine()
    import json as _json

    with engine.begin() as conn:
        control_plane._set_org(conn, org_id)
        conn.execute(
            sql(
                """
                INSERT INTO queued_actions (
                  org_id, bot_id, action_id, action, typed_json,
                  params_schema_json, risk, execution_route, origin_avatar,
                  permission_json, execution_status, execution_updated_at,
                  created_at, updated_at
                ) VALUES (
                  :org_id, :bot_id, :action_id, :action,
                  CAST(:typed AS jsonb), '[]'::jsonb, 'high', 'browser',
                  :avatar, CAST(:permission AS jsonb), 'proposed',
                  clock_timestamp(), clock_timestamp(), clock_timestamp()
                )
                ON CONFLICT (org_id, action_id) DO NOTHING
                """
            ),
            {"org_id": org_id, "bot_id": row.get("meeting_ref", ""),
             "action_id": action_id,
             "action": f"browser: {typed['args'].get('target', '')}"[:300],
             "typed": _json.dumps(typed, separators=(",", ":")),
             "avatar": row.get("avatar_key", ""),
             "permission": _json.dumps(
                 {"policy": "approval_required",
                  "browser_session_id": row["id"],
                  "binding_fingerprint": fingerprint,
                  "binding_element_id": element_id},
                 separators=(",", ":"))},
        )


# ── approved-execution path (existing claim + receipt) ──────────────────────

def execute_approved_step(org_id: str, action_id: str, action: dict) -> dict:
    """Called from the approve door for route='browser' actions, AFTER the M0
    execution claim is held. Re-checks browser-session ownership, state,
    resolved-avatar permission and tool allowance at EXECUTION time, then
    performs the guarded step against the provider. Writes a receipt through
    the same channel the dashboard reads. B0 uses the fake provider, so this
    proves the exactly-once + receipt path end to end."""
    from .. import ledger

    permission = action.get("permission_json") or action.get("permission") or {}
    if isinstance(permission, str):
        import json as _json

        try:
            permission = _json.loads(permission)
        except ValueError:
            permission = {}
    session_id = str(permission.get("browser_session_id") or "")
    row = dal.get_session_internal(org_id, session_id) if session_id else None
    if row is None:
        ledger.set_action_status(action_id, "failed",
                                 "browser session missing", org_id=org_id)
        return {"ok": False, "reason": "session_missing"}
    if row["state"] not in _COMMANDABLE:
        ledger.set_action_status(action_id, "failed",
                                 f"browser session {row['state']}",
                                 org_id=org_id)
        return {"ok": False, "reason": f"invalid_state:{row['state']}"}
    if not _browser_allowed(org_id, row["avatar_key"]):
        ledger.set_action_status(action_id, "failed",
                                 "browser tool not allowed", org_id=org_id)
        return {"ok": False, "reason": "browser_tool_not_allowed"}
    # Re-observe and RE-VERIFY the approval binding: the approval was bound to
    # a specific page state (url+title+element-ids fingerprint) and target
    # element. If the untrusted page changed since approval, the write is NOT
    # what the approver saw — fail closed. Enforced now (inert in B0's no-write
    # stance) so B1's real write inherits the guard rather than a stale one.
    provider = get_provider(row["provider"])
    try:
        current = policy.sanitize_observation(
            provider.observe(row["provider_ref"]))
    except Exception:  # noqa: BLE001
        ledger.set_action_status(action_id, "failed",
                                 "browser observe failed", org_id=org_id)
        return {"ok": False, "reason": "observe_failed"}
    bound_fp = str(permission.get("binding_fingerprint") or "")
    bound_el = str(permission.get("binding_element_id") or "")
    fresh_fp = _observation_fingerprint(current)
    target_present = (not bound_el) or bound_el in {
        str(e.get("id")) for e in current.get("elements") or []}
    if (bound_fp and fresh_fp != bound_fp) or not target_present:
        ledger.set_action_status(
            action_id, "failed", "browser page changed since approval",
            org_id=org_id,
            receipt={"kind": "browser", "route": "browser",
                     "session_id": session_id, "stale_page_binding": True})
        return {"ok": False, "reason": "stale_page_binding"}
    # B0: the guarded action is settled done WITHOUT performing a real
    # external write (fake provider, read-only stance) — the plane, the
    # re-checks, and the receipt are what B0 proves. B1 wires the real write.
    ledger.set_action_status(
        action_id, "done", "browser step approved (B0 read-only stance)",
        org_id=org_id,
        receipt={"kind": "browser", "route": "browser",
                 "session_id": session_id, "b0_no_write": True},
    )
    return {"ok": True, "receipt": "browser_b0"}
