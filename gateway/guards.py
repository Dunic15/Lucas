"""Development-only safeguards for the OpenClaw gateway package.

Every operation in this package (deploy, backup, restore, teardown, and the
laura-backend-next integration step) routes its target identifiers through
``assert_dev_only`` first. The rule is deliberately blunt: if a target names a
CUSTOMER resource, the operation refuses. There is no override flag, because
the failure mode being prevented — pointing a development gateway at the
customers' backend, branch, or database — is not recoverable by git.

The subtle part, and the reason this is a module rather than a grep:

    ``laura-backend``       is the CUSTOMERS' App Runner service   -> REJECT
    ``laura-backend-next``  is the development service              -> ALLOW

``laura-backend`` is a strict prefix of ``laura-backend-next``, so a naive
substring test rejects the only service we are allowed to touch. Matching is
therefore done on token boundaries, not on ``in``.

Nothing here talks to AWS. It is pure validation so it runs key-free in CI.
"""
from __future__ import annotations

import re

__all__ = [
    "DevOnlyViolation", "assert_dev_only", "check", "ALLOWED_SERVICE",
    "FORBIDDEN_SERVICES", "FORBIDDEN_BRANCHES", "FORBIDDEN_PATHS",
]

# The one service this package may ever configure.
ALLOWED_SERVICE = "laura-backend-next"

# Customer resources. Exact tokens — never substrings (see module docstring).
FORBIDDEN_SERVICES = frozenset({
    "laura-backend",          # customers' App Runner service
})
FORBIDDEN_BRANCHES = frozenset({
    "frozen/v1",              # the pinned customer branch
})
FORBIDDEN_PATHS = frozenset({
    "relay/cedric-voice",     # customers' voice bridge (ours is -v2)
})
# Hosts/identifiers that mean "customer production".
FORBIDDEN_SUBSTRINGS = (
    "app.lauravatar.com",     # customer dashboard
)
# Scripts that rewrite live ElevenLabs agents customers are talking to.
FORBIDDEN_COMMANDS = ("create_meeting_agent.py",)

_TOKEN = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", re.IGNORECASE)


class DevOnlyViolation(RuntimeError):
    """A target named a customer resource. Never catch this to continue."""


def _tokens(value: str) -> set[str]:
    """Identifier-ish tokens, plus progressively shorter dash-prefixes.

    ``laura-backend-next`` yields {laura, laura-backend, laura-backend-next,
    ...}. That is exactly why membership alone cannot decide: the allowed
    service CONTAINS the forbidden one as a prefix, so the caller must be
    matched on its own full identity first (see ``check``).
    """
    out: set[str] = set()
    for match in _TOKEN.findall(value or ""):
        lowered = match.lower()
        out.add(lowered)
        parts = lowered.split("-")
        for i in range(1, len(parts)):
            out.add("-".join(parts[:i]))
    return out


def check(*targets: str) -> list[str]:
    """Return the reasons these targets are forbidden ([] when they are fine)."""
    reasons: list[str] = []
    for raw in targets:
        value = str(raw or "").strip()
        if not value:
            continue
        lowered = value.lower()

        # 1. Whole-value allowlist short-circuit: the development service is
        #    explicitly permitted even though it contains a forbidden prefix.
        if lowered == ALLOWED_SERVICE:
            continue

        # 2. Exact-token service match. A value is rejected when one of its
        #    tokens IS a forbidden service and the value is not the allowed
        #    service — so "laura-backend" and "arn:...:service/laura-backend"
        #    are caught, while "laura-backend-next" is not.
        tokens = _tokens(value)
        for service in FORBIDDEN_SERVICES:
            if service in tokens and not _is_allowed_variant(lowered, service):
                reasons.append(
                    f"{value!r} names the customer service {service!r}"
                )
                break

        for branch in FORBIDDEN_BRANCHES:
            if branch in lowered:
                reasons.append(f"{value!r} names the customer branch {branch!r}")

        for path in FORBIDDEN_PATHS:
            # relay/cedric-voice must not match relay/cedric-voice-v2
            if re.search(rf"(?<![\w-]){re.escape(path)}(?![\w-])", lowered):
                reasons.append(f"{value!r} names the customer path {path!r}")

        for needle in FORBIDDEN_SUBSTRINGS:
            if needle in lowered:
                reasons.append(f"{value!r} names customer infrastructure "
                               f"({needle})")

        for command in FORBIDDEN_COMMANDS:
            if command in lowered:
                reasons.append(
                    f"{value!r} invokes {command!r}, which rewrites live "
                    "ElevenLabs agents — git cannot undo that"
                )
    return reasons


def _is_allowed_variant(value: str, service: str) -> bool:
    """True when `value` is the ALLOWED service (or an ARN/URL naming it)
    rather than the forbidden one it is prefixed by."""
    return bool(
        re.search(rf"(?<![\w-]){re.escape(ALLOWED_SERVICE)}(?![\w-])", value)
    ) and not re.search(rf"(?<![\w-]){re.escape(service)}(?![\w-])", value)


def assert_dev_only(*targets: str) -> None:
    """Raise unless every target is development-safe. No override exists."""
    reasons = check(*targets)
    if reasons:
        raise DevOnlyViolation(
            "development-only guard refused this operation:\n  - "
            + "\n  - ".join(reasons)
            + f"\nOnly {ALLOWED_SERVICE!r} may be configured by this package."
        )


if __name__ == "__main__":  # pragma: no cover - operator convenience
    import sys

    try:
        assert_dev_only(*sys.argv[1:])
    except DevOnlyViolation as exc:
        print(f"REFUSED\n{exc}")
        raise SystemExit(2)
    print(f"OK — targets are development-only ({ALLOWED_SERVICE})")
