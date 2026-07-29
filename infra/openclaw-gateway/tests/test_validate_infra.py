#!/usr/bin/env python3
"""Pytest wrapper so `pytest infra/openclaw-gateway/tests/` runs the static
infra validation too (validate_infra.py is also runnable standalone)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import validate_infra  # noqa: E402


def test_all_infra_invariants_hold():
    fails = validate_infra.run()
    assert fails == [], "infra validation failures:\n" + "\n".join(fails)
