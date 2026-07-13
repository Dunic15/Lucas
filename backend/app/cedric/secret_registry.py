"""Durable, hot-reloadable per-org callback signing registry.

The App Runner env value is only a boot-time snapshot.  SSM is authoritative:
Connect-the-brain merges the newly minted secret into the SecureString and
updates this process immediately; callback signing periodically refreshes the
cache so another process can observe changes without a deploy.

Secret values are never logged or returned by this module.
"""
from __future__ import annotations

import json
import re
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
# Force an SSM read on the FIRST callback after boot: App Runner resolves the
# env snapshot at deploy time, but a connect that landed in SSM while a previous
# process was alive can be newer than this process's boot env until SSM is
# consulted.  -inf makes the first secret_for() read SSM once (a single
# background-path call, never the live speak path), then it refreshes every N s.
_last_ssm_refresh = float("-inf")
_bearer_cache: dict[str, str] = {}
_bearer_last_refresh: dict[str, float] = {}


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


def _org_parameter_name(base: str, org_id: str) -> str:
    """Dedicated per-org signing-secret parameter."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", org_id):
        return ""
    return f"{base.rstrip('/')}/orgs/{org_id}"


def _bearer_parameter_name(base: str, org_id: str) -> str:
    """Dedicated Cedric→Laura peer bearer, kept separate from HMAC secrets."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", org_id):
        return ""
    return f"{base.rstrip('/')}/bearers/{org_id}"


def _read_aggregate_locked(client) -> dict[str, str] | None:
    name = settings.laura_webhook_registry_ssm_parameter.strip()
    if not name or client is None:
        return None
    response = client.get_parameter(Name=name, WithDecryption=True)
    raw = str((response.get("Parameter") or {}).get("Value") or "")
    return _parse(raw)


def _read_dedicated_locked(client) -> dict[str, str] | None:
    base = settings.laura_webhook_registry_ssm_parameter.strip()
    if not base or client is None or not hasattr(client, "get_parameters_by_path"):
        return None
    path = f"{base.rstrip('/')}/orgs"
    found: dict[str, str] = {}
    token = None
    while True:
        kwargs = {"Path": path, "Recursive": False, "WithDecryption": True}
        if token:
            kwargs["NextToken"] = token
        response = client.get_parameters_by_path(**kwargs)
        for parameter in response.get("Parameters") or []:
            name = str(parameter.get("Name") or "")
            org_id = name.rsplit("/", 1)[-1].strip()
            secret = str(parameter.get("Value") or "").strip()
            if _org_parameter_name(base, org_id) == name and secret:
                found[org_id] = secret
        token = response.get("NextToken")
        if not token:
            return found


def _read_ssm_locked(client) -> dict[str, str] | None:
    # Read the two sources independently. The legacy aggregate can be missing,
    # malformed, or temporarily unreadable while the authoritative per-org
    # SecureStrings remain healthy (notably after a process restart).
    try:
        aggregate = _read_aggregate_locked(client)
    except Exception as exc:  # noqa: BLE001 - still try dedicated parameters
        aggregate = None
        print(
            "[cedric-callback] aggregate SSM registry refresh failed "
            f"({type(exc).__name__})",
            flush=True,
        )
    try:
        dedicated = _read_dedicated_locked(client)
    except Exception as exc:  # noqa: BLE001 - aggregate may still be usable
        dedicated = None
        print(
            "[cedric-callback] dedicated SSM registry refresh failed "
            f"({type(exc).__name__})",
            flush=True,
        )
    if aggregate is None and dedicated is None:
        return None
    # Dedicated values win over the legacy JSON aggregate. They are written
    # independently, so concurrent org connects and rolling deploy overlap
    # cannot clobber one another.
    return {**(aggregate or {}), **(dedicated or {})}


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


def bearer_for(org_id: str) -> str:
    """Return the per-org bearer Laura must send to Cedric.

    The value is hot-cached but remains authoritative in one dedicated SSM
    SecureString per org. There is deliberately no global-token fallback here:
    callers decide explicitly whether a demo/legacy request may use one.
    """
    global _bearer_cache, _bearer_last_refresh
    org = (org_id or "").strip()
    if not org:
        return ""
    name = settings.laura_webhook_registry_ssm_parameter.strip()
    dedicated = _bearer_parameter_name(name, org) if name else ""
    with _lock:
        if not name or not dedicated:
            return _bearer_cache.get(org, "")
        now = time.monotonic()
        interval = max(1.0, settings.laura_webhook_registry_refresh_seconds)
        if now - _bearer_last_refresh.get(org, float("-inf")) < interval:
            return _bearer_cache.get(org, "")
        _bearer_last_refresh[org] = now
        client = _client()
        if client is None:
            return _bearer_cache.get(org, "")
        try:
            response = client.get_parameter(Name=dedicated, WithDecryption=True)
            value = str((response.get("Parameter") or {}).get("Value") or "").strip()
            if value:
                _bearer_cache = {**_bearer_cache, org: value}
            else:
                _bearer_cache = {k: v for k, v in _bearer_cache.items() if k != org}
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ParameterNotFound":
                _bearer_cache = {k: v for k, v in _bearer_cache.items() if k != org}
            else:
                print(
                    "[cedric-callback] per-org bearer refresh failed "
                    f"({type(exc).__name__})",
                    flush=True,
                )
        return _bearer_cache.get(org, "")


def upsert_org_bearer(org_id: str, bearer: str) -> bool:
    """Persist Cedric's per-workspace bearer and hot-update this process."""
    global _bearer_cache, _bearer_last_refresh
    org = (org_id or "").strip()
    value = (bearer or "").strip()
    name = settings.laura_webhook_registry_ssm_parameter.strip()
    dedicated = _bearer_parameter_name(name, org) if name else ""
    if not org or not value:
        return False
    with _lock:
        if not name:
            _bearer_cache = {**_bearer_cache, org: value}
            _bearer_last_refresh[org] = time.monotonic()
            return True
        if not dedicated:
            return False
        client = _client()
        if client is None:
            return False
        try:
            client.put_parameter(
                Name=dedicated, Value=value, Type="SecureString", Overwrite=True
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "[cedric-callback] per-org bearer update failed "
                f"({type(exc).__name__})",
                flush=True,
            )
            return False
        _bearer_cache = {**_bearer_cache, org: value}
        _bearer_last_refresh[org] = time.monotonic()
        return True


def upsert_org_credentials(org_id: str, secret: str, bearer: str) -> bool:
    """Persist both directions of the Cedric workspace credential contract."""
    if not upsert_org_secret(org_id, secret):
        return False
    return upsert_org_bearer(org_id, bearer)


def upsert_org_secret(org_id: str, secret: str) -> bool:
    """Merge one org into the SSM SecureString and hot-update this process.

    The dedicated per-org SecureString is authoritative and cannot clobber a
    different org. The legacy aggregate is still merged for compatibility.
    The process-wide lock also serializes requests within this instance.
    """
    global _cache, _last_ssm_refresh
    org = (org_id or "").strip()
    value = (secret or "").strip()
    name = settings.laura_webhook_registry_ssm_parameter.strip()
    dedicated_name = _org_parameter_name(name, org)
    if not org or not value or not name or not dedicated_name:
        return False
    with _lock:
        _sync_env_locked()
        try:
            client = _client()
            if client is None:
                return False
            # This is the durable write. It touches one org only, so another
            # process can never lose it while updating a different org.
            client.put_parameter(
                Name=dedicated_name,
                Value=value,
                Type="SecureString",
                Overwrite=True,
            )
            merged = dict(_cache)
            merged[org] = value
            # Keep the contract's aggregate parameter current when it is valid.
            # A concurrent aggregate write may be stale, but callback reads
            # always overlay the authoritative dedicated parameters above.
            try:
                current = _read_aggregate_locked(client)
                if current is not None:
                    aggregate = dict(current)
                    aggregate[org] = value
                    client.put_parameter(
                        Name=name,
                        Value=json.dumps(
                            aggregate, separators=(",", ":"), sort_keys=True
                        ),
                        Type="SecureString",
                        Overwrite=True,
                    )
            except Exception as exc:  # noqa: BLE001 - durable write succeeded
                print(
                    "[cedric-callback] aggregate SSM registry refresh failed "
                    f"({type(exc).__name__})",
                    flush=True,
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


def remove_org_credentials(org_id: str) -> bool:
    """Remove every per-org credential durably and retry-safely.

    When SSM is configured, hot caches are evicted only after every durable
    delete succeeds. A partial failure therefore leaves the peer bearer
    available for the disconnect saga to retry; the dashboard persists the
    remote-revoked phase before invoking this cleanup.
    """
    global _cache, _last_ssm_refresh, _bearer_cache, _bearer_last_refresh
    org = (org_id or "").strip()
    if not org:
        return False
    name = settings.laura_webhook_registry_ssm_parameter.strip()
    with _lock:
        _sync_env_locked()
        if not name:
            _cache = {k: v for k, v in _cache.items() if k != org}
            _bearer_cache = {k: v for k, v in _bearer_cache.items() if k != org}
            _bearer_last_refresh.pop(org, None)
            return True

        secret_name = _org_parameter_name(name, org)
        bearer_name = _bearer_parameter_name(name, org)
        client = _client()
        if client is None or not secret_name or not bearer_name:
            return False

        ok = True
        for parameter in (secret_name, bearer_name):
            try:
                client.delete_parameter(Name=parameter)
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ != "ParameterNotFound":
                    ok = False
                    print(
                        "[cedric-callback] credential SSM delete failed "
                        f"({type(exc).__name__})",
                        flush=True,
                    )
        try:
            current = _read_aggregate_locked(client)
            if current is not None and org in current:
                current.pop(org)
                client.put_parameter(
                    Name=name,
                    Value=json.dumps(
                        current, separators=(",", ":"), sort_keys=True
                    ),
                    Type="SecureString",
                    Overwrite=True,
                )
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ != "ParameterNotFound":
                ok = False
                print(
                    "[cedric-callback] aggregate SSM registry delete failed "
                    f"({type(exc).__name__})",
                    flush=True,
                )

        if ok:
            _cache = {k: v for k, v in _cache.items() if k != org}
            _bearer_cache = {k: v for k, v in _bearer_cache.items() if k != org}
            _bearer_last_refresh.pop(org, None)
            _last_ssm_refresh = time.monotonic()
        return ok

def remove_org_secret(org_id: str) -> bool:
    """Backward-compatible alias; disconnect now removes both credentials."""
    return remove_org_credentials(org_id)
