#!/usr/bin/env python3
"""The dev-only guard must let dev targets through and reject customer ones.

Key-free, stdlib-only. Runs under pytest or standalone (python3 test_dev_guard.py).
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import dev_guard  # noqa: E402

# Names that MUST be rejected (customer / frozen production line).
CUSTOMER = [
    "laura-backend",             # the frozen/v1 App Runner service customers run
    "laura-backend-prod",
    "laura-backend-frozen",
    "frozen-v1",
    "laura-frozen/v1",
    "dhfgfe6yw6",                # customer App Runner host id
]

# Names that MUST be accepted (sanctioned dev targets).
DEV = [
    "laura-openclaw-gw-dev",
    "laura-openclaw-gw-dev-eu",
    "laura-backend-next",        # the DEV backend service (env-merge target)
    "48zmdue8kg",                # dev App Runner host id
    "openclaw-sandbox",
    "/laura/dev/openclaw",
]

# Ambiguous names with no dev marker MUST fail closed.
UNKNOWN = [
    "some-service",
    "laura-gateway",
    "prod-thing",
]


def test_customer_names_rejected():
    for name in CUSTOMER:
        assert dev_guard.is_customer_resource(name), f"should flag customer: {name}"
        try:
            dev_guard.assert_dev_only(name)
        except dev_guard.CustomerResourceError:
            continue
        raise AssertionError(f"assert_dev_only should have rejected {name}")


def test_dev_names_accepted():
    for name in DEV:
        assert not dev_guard.is_customer_resource(name), f"dev misflagged: {name}"
        assert dev_guard.assert_dev_only(name) == name


def test_unknown_names_fail_closed():
    for name in UNKNOWN:
        try:
            dev_guard.assert_dev_only(name)
        except dev_guard.CustomerResourceError:
            continue
        raise AssertionError(f"unknown name should fail closed: {name}")


def test_laura_backend_next_not_confused_with_customer():
    # The substring trap: laura-backend-next is DEV, laura-backend is CUSTOMER.
    assert dev_guard.is_customer_resource("laura-backend")
    assert not dev_guard.is_customer_resource("laura-backend-next")


if __name__ == "__main__":
    failures = 0
    for fn in (
        test_customer_names_rejected,
        test_dev_names_accepted,
        test_unknown_names_fail_closed,
        test_laura_backend_next_not_confused_with_customer,
    ):
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
