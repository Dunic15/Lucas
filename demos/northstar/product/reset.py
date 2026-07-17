"""One reset command:  python -m demos.northstar.product.reset

Restores the demo product to its frozen seed. Safe to run between
demonstrations and idempotent (two resets == one reset). Prints the resulting
task count so a presenter can confirm the world is clean.
"""
from __future__ import annotations

from . import store


def main() -> int:
    store.reset()
    print(f"northstar demo reset -> {len(store.tasks())} seed tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
