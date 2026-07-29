#!/usr/bin/env python3
"""Dev-only deployment guard.

Every mutating helper (deploy, teardown, secrets, the App Runner env merge)
routes its target name through here FIRST. The guard fails closed: it raises
unless the name is recognisably a development resource, and it raises loudly on
any known customer resource. This is what keeps `frozen/v1` and the customer
`laura-backend` service untouchable from this toolkit.

Pure stdlib, key-free — importable by the validation tests and runnable as a CLI:

    python3 dev_guard.py <resource-name> [<resource-name> ...]

Exit 0 = every name is a safe dev target. Exit 2 = at least one is rejected.
"""
from __future__ import annotations

import re
import sys

# Resources that belong to customers / the frozen production line. Targeting any
# of these from this dev toolkit is always an error.
CUSTOMER_DENY_EXACT = {
    "laura-backend",          # frozen/v1 App Runner service that customers run
    "dhfgfe6yw6",             # its App Runner host id (from README)
}
CUSTOMER_DENY_SUBSTRINGS = (
    "frozen",                 # frozen/v1, frozen-v1, ...
)

# The sanctioned development targets.
DEV_ALLOW_EXACT = {
    "laura-backend-next",     # the DEV App Runner service (main deploys here)
    "48zmdue8kg",             # its App Runner host id (from README)
}
# Anything created by this stack must carry the dev gateway prefix.
DEV_ALLOW_PREFIXES = (
    "laura-openclaw-gw-dev",
)
# A generic "looks like dev" escape hatch for stack/log/volume names.
DEV_MARKER = re.compile(r"(?:^|[-_/])(?:dev|test|sandbox|staging)(?:$|[-_/])", re.I)


class CustomerResourceError(RuntimeError):
    """Raised when a name is (or resembles) a protected customer resource."""


def _norm(name: str) -> str:
    return str(name or "").strip()


def is_customer_resource(name: str) -> bool:
    n = _norm(name).lower()
    if not n:
        return False
    if n in {x.lower() for x in CUSTOMER_DENY_EXACT}:
        return True
    # `laura-backend` (and derivations) are customer — EXCEPT `laura-backend-next`.
    if n.startswith("laura-backend") and not n.startswith("laura-backend-next"):
        return True
    return any(s in n for s in CUSTOMER_DENY_SUBSTRINGS)


def is_dev_resource(name: str) -> bool:
    n = _norm(name)
    if not n or is_customer_resource(n):
        return False
    low = n.lower()
    if low in {x.lower() for x in DEV_ALLOW_EXACT}:
        return True
    if any(low.startswith(p) for p in DEV_ALLOW_PREFIXES):
        return True
    return bool(DEV_MARKER.search(n))


def assert_dev_only(name: str) -> str:
    """Return the name if it is a safe dev target, else raise."""
    n = _norm(name)
    if is_customer_resource(n):
        raise CustomerResourceError(
            f"REFUSED: '{n}' is a customer/frozen resource. This dev toolkit "
            f"must never target it. Customer denylist: "
            f"{sorted(CUSTOMER_DENY_EXACT)} + *{CUSTOMER_DENY_SUBSTRINGS}*."
        )
    if not is_dev_resource(n):
        raise CustomerResourceError(
            f"REFUSED: '{n}' is not recognisably a development resource. "
            f"Use a name with the '{DEV_ALLOW_PREFIXES[0]}' prefix or an explicit "
            f"dev/test/sandbox marker. (Fails closed on unknown names.)"
        )
    return n


def main(argv: list[str]) -> int:
    names = argv[1:]
    if not names:
        print("usage: dev_guard.py <resource-name> [...]", file=sys.stderr)
        return 2
    rc = 0
    for name in names:
        try:
            assert_dev_only(name)
            print(f"OK   {name}")
        except CustomerResourceError as exc:
            print(str(exc), file=sys.stderr)
            rc = 2
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
