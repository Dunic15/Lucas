"""Deterministic dependency rules for OpenClaw runs.

Pure functions only — no database, no network, no settings — so the
dependency contract can be unit tested without booting the FastAPI app.

Three vocabularies meet here, so be explicit about which one this module
speaks:

* ``depends_on`` on a *canonical* action holds **action_id strings**. Chat
  drafts use 1-based step indexes, but ``start_chat_workflow`` converts them
  to action ids before the run payload is written, so by the time anything
  here runs the ids are canonical.
* prerequisite state comes from ``openclaw_action_runs.status``.
* a dependent that can never run is settled ``needs_attention``, not
  ``failed`` (nothing was ever attempted against the vendor, so nothing
  failed) and not ``cancelled`` — ``_finish_openresponses_run`` reads a run
  whose actions are all ``done``/``cancelled`` as a SUCCESS, so settling a
  blocked dependent ``cancelled`` would quietly repaint a broken chain as a
  completed run. ``needs_attention`` is terminal, truthful, and keeps the
  run's own terminal status honest.

The gateway is a language model: it is *told* to call actions in ascending
sequence, but nothing stops it from calling step 3 first, twice, or after
step 1 failed. Every rule here is enforced server-side, before the vendor
call, and never depends on gateway call order.
"""

from __future__ import annotations

# A prerequisite is satisfied only by a completed action.
SATISFIED_STATUS = "done"

# Prerequisite states that can still become ``done`` later: the dependent is
# early, not doomed.
PENDING_STATUSES = ("queued", "planning", "running")

# Prerequisite states that make the dependent unrunnable for good.
BLOCKING_STATUSES = ("failed", "cancelled", "needs_attention")

# Terminal status written on a dependent we refuse to execute. NOT
# ``cancelled``: see the module docstring — ``cancelled`` reads as success at
# the run level.
BLOCKED_STATUS = "needs_attention"

# Gate decisions.
ALLOW = "allow"
WAIT = "wait"
BLOCKED = "blocked"

# Bound on how much of a dependency plan we will describe in an error string.
_MAX_NAMED_BLOCKERS = 5


def normalize_depends_on(action: dict) -> list[str]:
    """Canonical, de-duplicated ``depends_on`` list for one action.

    Accepts the legacy ``dependencies`` spelling on input (``_sanitize_action``
    does the same) but callers should always *write* ``depends_on``.
    """
    source = action if isinstance(action, dict) else {}
    raw = source.get("depends_on")
    if not raw:
        raw = source.get("dependencies")
    out: list[str] = []
    seen: set[str] = set()
    for value in raw or []:
        dep = str(value).strip()
        if dep and dep not in seen:
            seen.add(dep)
            out.append(dep)
    return out


def _reaches_start(start: str, edges: dict[str, list[str]]) -> list[str]:
    """Return a cycle path through ``start``, or ``[]`` when there is none."""
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    visited: set[str] = set()
    while stack:
        node, path = stack.pop()
        for dep in edges.get(node, ()):
            if dep == start:
                return path + [start]
            if dep in visited:
                continue
            visited.add(dep)
            stack.append((dep, path + [dep]))
    return []


def validate_dependency_graph(actions: list[dict]) -> dict[str, str]:
    """Reject structurally invalid dependency plans, fail-closed.

    Returns ``{action_id: reason}`` for every action that must never be
    exposed to the gateway or executed. An empty dict means the plan is safe.

    Rejected: duplicate action ids, self-dependencies, unknown dependencies,
    cycles, and forward dependencies (depending on a later step). Anything
    that transitively depends on a rejected action is rejected too — a
    prerequisite that will never run can never be satisfied.
    """
    ids: list[str] = []
    position: dict[str, int] = {}
    duplicates: set[str] = set()
    for index, raw in enumerate(actions or []):
        action = raw if isinstance(raw, dict) else {}
        aid = str(action.get("action_id") or "").strip()
        ids.append(aid)
        if not aid:
            continue
        if aid in position:
            duplicates.add(aid)
        else:
            position[aid] = index

    problems: dict[str, str] = {}
    edges: dict[str, list[str]] = {}

    for index, raw in enumerate(actions or []):
        aid = ids[index]
        if not aid:
            continue
        if aid in duplicates:
            problems[aid] = "duplicate action_id in the same run"
            continue
        action = raw if isinstance(raw, dict) else {}
        deps = normalize_depends_on(action)
        edges[aid] = [dep for dep in deps if dep in position and dep != aid]
        for dep in deps:
            if dep == aid:
                problems[aid] = "action depends on itself"
                break
            if dep not in position:
                problems[aid] = f"unknown dependency {dep!r}"
                break

    for aid in edges:
        if aid in problems:
            continue
        cycle = _reaches_start(aid, edges)
        if cycle:
            problems[aid] = "circular dependency: " + " -> ".join(cycle)

    for aid, deps in edges.items():
        if aid in problems:
            continue
        for dep in deps:
            if position[dep] >= position[aid]:
                problems[aid] = (
                    f"dependency {dep!r} is declared after this action"
                )
                break

    # A dependent of a rejected action can never see that action reach
    # ``done``, so it is rejected too. Iterate to a fixpoint.
    changed = True
    while changed:
        changed = False
        for aid, deps in edges.items():
            if aid in problems:
                continue
            for dep in deps:
                if dep in problems:
                    problems[aid] = f"depends on rejected action {dep!r}"
                    changed = True
                    break

    return problems


def describe_prerequisites(deps: list[str], statuses: dict[str, str]) -> str:
    """Human-readable ``id (status)`` list, bounded."""
    named = [
        f"{dep} ({str(statuses.get(dep) or 'unknown')})"
        for dep in deps[:_MAX_NAMED_BLOCKERS]
    ]
    extra = len(deps) - len(named)
    if extra > 0:
        named.append(f"and {extra} more")
    return ", ".join(named)


def gate(
    action: dict,
    statuses: dict[str, str],
) -> tuple[str, str, list[str]]:
    """Decide whether one action may execute right now.

    Returns ``(decision, reason, prerequisite_ids)`` where decision is:

    * ``ALLOW``   — every prerequisite is ``done``.
    * ``BLOCKED`` — a prerequisite reached a terminal non-done status, so the
      dependent must be settled without any vendor call.
    * ``WAIT``    — a prerequisite has simply not finished yet: the gateway
      asked out of order and gets a safe structured error, no write.
    """
    deps = normalize_depends_on(action)
    if not deps:
        return (ALLOW, "", [])

    blocked = [
        dep for dep in deps
        if str(statuses.get(dep) or "") in BLOCKING_STATUSES
    ]
    if blocked:
        return (
            BLOCKED,
            "blocked by unmet prerequisite: "
            + describe_prerequisites(blocked, statuses),
            blocked,
        )

    waiting = [
        dep for dep in deps
        if str(statuses.get(dep) or "") != SATISFIED_STATUS
    ]
    if waiting:
        return (
            WAIT,
            "prerequisite has not completed yet: "
            + describe_prerequisites(waiting, statuses),
            waiting,
        )

    return (ALLOW, "", [])


def dependents_of(actions: list[dict], action_ids: set[str]) -> list[str]:
    """Action ids that directly depend on any of ``action_ids``."""
    out: list[str] = []
    for raw in actions or []:
        action = raw if isinstance(raw, dict) else {}
        aid = str(action.get("action_id") or "").strip()
        if not aid or aid in action_ids:
            continue
        if set(normalize_depends_on(action)) & action_ids:
            out.append(aid)
    return out


def transitive_dependents(actions: list[dict], action_ids: set[str]) -> list[str]:
    """Every action that can no longer run because ``action_ids`` will not.

    Run order preserved, seeds excluded. Used to settle the tail of a broken
    chain eagerly, so a run whose step 1 failed reaches a terminal status even
    if the gateway never asks for steps 2 and 3 at all.

    Only actions that actually DECLARE a dependency (directly or through the
    chain) are returned: an unrelated sibling with an empty ``depends_on`` is
    never swept up by a neighbour's failure.
    """
    doomed = set(action_ids)
    order = [
        str((raw if isinstance(raw, dict) else {}).get("action_id") or "").strip()
        for raw in actions or []
    ]
    while True:
        found = dependents_of(actions, doomed)
        fresh = [aid for aid in found if aid not in doomed]
        if not fresh:
            break
        doomed.update(fresh)
    return [aid for aid in order if aid and aid in doomed and aid not in action_ids]
