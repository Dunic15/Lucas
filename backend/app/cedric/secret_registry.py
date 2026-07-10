"""Durable, hot-reloadable per-org callback signing registry.

The App Runner env value is only a boot-time snapshot.  SSM is authoritative:
Connect-the-brain merges the newly minted secret into the SecureString and
updates this process immediately; callback signing periodically refreshes the
cache so another process can observe changes without a deploy.

Secret values are never logged or returned by this module.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

try:  # local/key-free demos keep working without AWS extras installed
    import boto3
except ImportError:  # pragma: no cover - boto3 is present in production
    boto3 = None

from ..config import settings

_lock = threading.RLock()
_cache: dict[str, str] = {}
_env_snapshot: str | None = None
# Do not make an AWS metadata/network call on first local callback.  Production
# already has the env snapshot at boot; a connect writes through immediately.
_last_ssm_refresh = time.monotonic()


def _valid_registry(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    clean: dict[str, str] = {}
    for org_id, secret in value.items():
        if not isinstance(org_id, str) or not isinstance(secret, str):
            return None
        if org_id.strip() and secret.strip():
            clean[org_id.strip()] = secret.strip()
    return clean


def _parse(raw: str) -> dict[str, str] | None:
    if not raw.strip():
        return {}
    try:
        return _valid_registry(json.loads(raw))
    except ValueError:
        return None


def _sync_env_locked() -> None:
    global _cache, _env_snapshot
    raw = settings.laura_webhook_secrets_by_org.strip()
    if raw == _env_snapshot:
        return
    parsed = _parse(raw)
    _cache = parsed if parsed is not None else {}
    _env_snapshot = raw
    if parsed is None:
        print("[cedric-callback] per-org secret registry is invalid JSON", flush=True)


def _client():
    if boto3 is None:
        return None
    return boto3.client(
        "ssm", region_name=settings.laura_webhook_registry_aws_region.strip()
    )


def _read_ssm_locked(client) -> dict[str, str] | None:
    name = settings.laura_webhook_registry_ssm_parameter.strip()
    if not name or client is None:
        return None
    response = client.get_parameter(Name=name, WithDecryption=True)
    raw = str((response.get("Parameter") or {}).get("Value") or "")
    return _parse(raw)


def secret_for(org_id: str) -> str:
    """Return an org secret from a cache refreshed from SSM every N seconds."""
    global _cache, _last_ssm_refresh
    org = (org_id or "").strip()
    with _lock:
        _sync_env_locked()
        interval = max(1.0, settings.laura_webhook_registry_refresh_seconds)
        now = time.monotonic()
        if (
            settings.laura_webhook_registry_ssm_parameter.strip()
            and now - _last_ssm_refresh >= interval
        ):
            # Stamp before the call so a failing SSM endpoint cannot be hit on
            # every callback.  Keep the last good/env cache on any failure.
            _last_ssm_refresh = now
            try:
                refreshed = _read_ssm_locked(_client())
                if refreshed is not None:
                    _cache = refreshed
                else:
                    print("[cedric-callback] SSM registry unavailable", flush=True)
            except Exception as exc:  # noqa: BLE001 - callback falls back safely
                print(
                    "[cedric-callback] SSM registry refresh failed "
                    f"({type(exc).__name__})",
                    flush=True,
                )
        return _cache.get(org, "") if org else ""


def upsert_org_secret(org_id: str, secret: str) -> bool:
    """Merge one org into the SSM SecureString and hot-update this process.

    Existing org entries are preserved.  The process-wide lock also prevents
    concurrent Connect requests on the single App Runner instance from racing
    their read/merge/write cycles.
    """
    global _cache, _last_ssm_refresh
    org = (org_id or "").strip()
    value = (secret or "").strip()
    name = settings.laura_webhook_registry_ssm_parameter.strip()
    if not org or not value or not name:
        return False
    with _lock:
        _sync_env_locked()
        try:
            client = _client()
            current = _read_ssm_locked(client)
            # Invalid JSON is not safe to overwrite: fail pending instead of
            # clobbering a registry that needs operator inspection.
            if current is None or client is None:
                return False
            merged = dict(current)
            merged[org] = value
            client.put_parameter(
                Name=name,
                Value=json.dumps(merged, separators=(",", ":"), sort_keys=True),
                Type="SecureString",
                Overwrite=True,
            )
            _cache = merged
            _last_ssm_refresh = time.monotonic()
            return True
        except Exception as exc:  # noqa: BLE001 - connection stays pending
            print(
                "[cedric-callback] SSM registry update failed "
                f"({type(exc).__name__})",
                flush=True,
            )
            return False
