"""Vendor subscription/credit watchdog — "avvisami prima che scada".

Every avatar meeting stands on a stack of paid vendors; any one of them
expiring kills the product silently (the Google refresh token dying is the
classic: calendar auto-join just stops). Each check here returns a small
status dict; the daily loop in main.py posts the non-ok ones to Slack
(SLACK_WEBHOOK_URL) and GET /health/vendors serves the full picture on
demand.

Statuses: ok | warn (act soon) | crit (act now) | off (not configured —
deliberately not an alarm, the zero-key demo stays quiet). Checks are
best-effort and cheap (one HTTP call each); a vendor being DOWN reads as
crit with the error attached, never an exception — the watchdog must not
need a watchdog.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from ..config import settings

# Thresholds: warn = put it on this week's list, crit = it dies in days.
ELEVENLABS_WARN_PCT = 10.0
ELEVENLABS_CRIT_PCT = 3.0
RUNPOD_WARN_USD = 5.0
RUNPOD_CRIT_USD = 2.0

_client = httpx.Client(timeout=15.0)


def _status(service: str, status: str, detail: str, **extra: Any) -> dict:
    return {"service": service, "status": status, "detail": detail, **extra}


def check_elevenlabs() -> dict:
    """Voice credits: characters remaining + when the quota resets."""
    if not settings.elevenlabs_api_key:
        return _status("elevenlabs", "off", "ELEVENLABS_API_KEY non configurata")
    try:
        r = _client.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": settings.elevenlabs_api_key},
        )
        if r.status_code in (401, 403):
            return _status("elevenlabs", "crit", f"chiave non valida (HTTP {r.status_code})")
        r.raise_for_status()
        d = r.json()
        used = float(d.get("character_count", 0))
        limit = float(d.get("character_limit", 0)) or 1.0
        left_pct = max(0.0, (limit - used) / limit * 100.0)
        reset = d.get("next_character_count_reset_unix")
        reset_txt = (
            time.strftime("%Y-%m-%d", time.localtime(reset)) if reset else "n/d"
        )
        detail = (
            f"caratteri rimasti {left_pct:.0f}% "
            f"({int(limit - used):,}/{int(limit):,}), reset {reset_txt}"
        )
        status = (
            "crit" if left_pct < ELEVENLABS_CRIT_PCT
            else "warn" if left_pct < ELEVENLABS_WARN_PCT
            else "ok"
        )
        return _status("elevenlabs", status, detail, left_pct=round(left_pct, 1))
    except Exception as e:  # noqa: BLE001
        return _status("elevenlabs", "warn", f"check fallito: {e}")


def check_google_oauth() -> dict:
    """The refresh token behind calendar auto-join + gmail watch. When it
    expires/revokes, meetings silently stop being joined — the single most
    important check here."""
    try:
        from . import gmail_watcher

        rt = gmail_watcher.refresh_token()
        if not rt:
            return _status("google-oauth", "off", "account Google non collegato")
        gmail_watcher.access_token(rt)  # a refresh that fails = token dead
        return _status("google-oauth", "ok", "refresh token valido")
    except Exception as e:  # noqa: BLE001
        return _status(
            "google-oauth",
            "crit",
            f"refresh FALLITO — ricollegare via /oauth/google/connect ({e})",
        )


def check_recall() -> dict:
    """Meeting-bot vendor: the key going invalid = no avatar joins anything."""
    if not settings.recall_api_key:
        return _status("recall", "off", "RECALL_API_KEY non configurata")
    try:
        r = _client.get(
            f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/?limit=1",
            headers={"Authorization": f"Token {settings.recall_api_key}"},
        )
        if r.status_code in (401, 403):
            return _status("recall", "crit", f"chiave non valida (HTTP {r.status_code})")
        r.raise_for_status()
        return _status("recall", "ok", "chiave valida")
    except Exception as e:  # noqa: BLE001
        return _status("recall", "warn", f"check fallito: {e}")


def check_groq() -> dict:
    """Fast-brain provider (Groq/Cerebras — GROQ_BASE decides which)."""
    if not settings.groq_api_key:
        return _status("llm-fast", "off", "GROQ_API_KEY non configurata")
    try:
        r = _client.get(
            f"{settings.groq_base.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        )
        if r.status_code in (401, 403):
            return _status("llm-fast", "crit", f"chiave non valida (HTTP {r.status_code})")
        r.raise_for_status()
        return _status("llm-fast", "ok", "chiave valida")
    except Exception as e:  # noqa: BLE001
        return _status("llm-fast", "warn", f"check fallito: {e}")


def check_anthropic() -> dict:
    if not settings.anthropic_api_key:
        return _status("anthropic", "off", "ANTHROPIC_API_KEY non configurata")
    try:
        r = _client.get(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        if r.status_code in (401, 403):
            return _status("anthropic", "crit", f"chiave non valida (HTTP {r.status_code})")
        r.raise_for_status()
        return _status("anthropic", "ok", "chiave valida")
    except Exception as e:  # noqa: BLE001
        return _status("anthropic", "warn", f"check fallito: {e}")


def check_runpod() -> dict:
    """Photoreal-GPU credit. RunPod bills from a prepaid balance — at $0 the
    pod (and the photoreal face) just stops."""
    if not settings.runpod_api_key:
        return _status("runpod", "off", "RUNPOD_API_KEY non configurata")
    try:
        r = _client.post(
            f"https://api.runpod.io/graphql?api_key={settings.runpod_api_key}",
            json={"query": "query { myself { clientBalance } }"},
            # Cloudflare in front of api.runpod.io bans the default UA
            # (see runpod_runtime.py).
            headers={"User-Agent": "laura-backend/1.0"},
        )
        if r.status_code in (401, 403):
            return _status("runpod", "crit", f"chiave non valida (HTTP {r.status_code})")
        r.raise_for_status()
        balance = float(
            (r.json().get("data") or {}).get("myself", {}).get("clientBalance", 0.0)
        )
        status = (
            "crit" if balance < RUNPOD_CRIT_USD
            else "warn" if balance < RUNPOD_WARN_USD
            else "ok"
        )
        return _status(
            "runpod", status, f"credito ${balance:.2f}", balance_usd=round(balance, 2)
        )
    except Exception as e:  # noqa: BLE001
        return _status("runpod", "warn", f"check fallito: {e}")


_CHECKS: tuple[Callable[[], dict], ...] = (
    check_elevenlabs,
    check_google_oauth,
    check_recall,
    check_groq,
    check_anthropic,
    check_runpod,
)

# Last full run, served by GET /health/vendors between daily sweeps.
last_results: list[dict] = []
last_run_at: float = 0.0


def run_checks() -> list[dict]:
    """Run every vendor check (each is exception-proof). Caches the result."""
    global last_results, last_run_at
    results = [c() for c in _CHECKS]
    last_results, last_run_at = results, time.time()
    return results


def slack_text(results: list[dict]) -> str:
    """Alert body: only the actionable items, ok/off summarized in one line.
    Returns "" when everything is fine (caller then posts nothing)."""
    icons = {"crit": "🔴", "warn": "🟡"}
    bad = [r for r in results if r["status"] in icons]
    if not bad:
        return ""
    lines = ["⚠️ *Laura — controllo abbonamenti/crediti*"]
    for r in sorted(bad, key=lambda r: r["status"]):  # crit first
        lines.append(f"{icons[r['status']]} *{r['service']}*: {r['detail']}")
    ok_n = sum(1 for r in results if r["status"] == "ok")
    lines.append(f"_(altri {ok_n} servizi ok)_")
    return "\n".join(lines)
