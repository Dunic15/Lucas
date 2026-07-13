"""Alembic must never silently migrate with the runtime credential."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _alembic_current(runtime_url: str, admin_url: str, require: bool = False):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=str(BACKEND_DIR),
        env={
            **os.environ,
            "LAURA_DATABASE_URL": runtime_url,
            "LAURA_DATABASE_ADMIN_URL": admin_url,
            "LAURA_REQUIRE_MIGRATIONS": "1" if require else "0",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_runtime_url_without_admin_url_fails_loudly_without_leaking_url():
    secret_marker = "runtime-password-must-not-appear"
    proc = _alembic_current(
        f"postgresql://laura_app:{secret_marker}@runtime.invalid/laura",
        "",
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "LAURA_DATABASE_ADMIN_URL is required" in output
    assert secret_marker not in output
    assert "runtime.invalid" not in output


def test_both_urls_empty_is_the_key_free_noop():
    proc = _alembic_current("", "")
    assert proc.returncode == 0
    assert "key-free SQLite demo" in (proc.stdout + proc.stderr)

def test_required_migration_job_fails_when_both_urls_are_empty():
    proc = _alembic_current("", "", require=True)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "LAURA_DATABASE_ADMIN_URL is required" in output
    assert "key-free SQLite demo" not in output

