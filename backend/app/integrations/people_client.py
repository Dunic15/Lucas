"""Google People (Contacts) client — read-only who's-who lookup.

Resolves a spoken name ("email Duccio") to an address from the connected
account's contacts: saved contacts (``people.searchContacts``) merged with
"other contacts" (``otherContacts.search`` — everyone the account has ever
emailed, usually the richer set for an owner's assistant).

Same contract as the rest of the integrations package: org-scoped, soft-fail
(``{"ok": False, "error": ...}``, never raises), and NOTHING here is ever
logged — contact names/addresses are personal data. Token comes from
google_client's seam (no cache of our own, so no conftest reset needed).
Requires the contacts.readonly + contacts.other.readonly scopes; connections
made before the scope bundle need one reconnect — surfaced as a distilled
"needs reconnect" error, never a raw Google body.

Never on the live hot path: the live tool call is the user asking for a
lookup mid-meeting (bounded timeout); ``warmup`` runs at session start
because Google serves stale/empty search results until a warmup request
primes each index (documented People API behaviour).
"""
from __future__ import annotations

import httpx

from . import google_client

_SEARCH_CONTACTS = "https://people.googleapis.com/v1/people:searchContacts"
_SEARCH_OTHER = "https://people.googleapis.com/v1/otherContacts:search"
_READ_MASK = "names,emailAddresses"
_TIMEOUT = 8.0
_MAX_RESULTS = 8


def _entries(payload: dict) -> list[dict]:
    """Flatten a People search response into [{name, email}] rows."""
    out: list[dict] = []
    for r in (payload or {}).get("results") or []:
        person = (r or {}).get("person") or {}
        names = person.get("names") or []
        emails = person.get("emailAddresses") or []
        display = str((names[0] or {}).get("displayName") or "").strip() if names else ""
        for e in emails:
            addr = str((e or {}).get("value") or "").strip()
            if addr:
                out.append({"name": display, "email": addr})
    return out


def _get(url: str, token: str, params: dict) -> httpx.Response:
    return httpx.get(
        url, headers={"Authorization": f"Bearer {token}"},
        params=params, timeout=_TIMEOUT,
    )


def search_contacts(org_id: str, query: str) -> dict:
    """Search saved + other contacts for a name/address fragment.

    Returns ``{ok: True, results: [{name, email}]}`` (deduped by address,
    capped) or ``{ok: False, error}``. A 403 after one forced re-mint means
    the grant itself is missing (pre-scope-bundle token) → a distilled
    reconnect message, never a raw response body."""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "empty query"}
    token, err = google_client._access_token(org_id)
    if err:
        return {"ok": False, "error": err}

    def _both(tok: str) -> tuple[list[dict], int]:
        rows: list[dict] = []
        worst = 200
        for url in (_SEARCH_CONTACTS, _SEARCH_OTHER):
            resp = _get(url, tok, {"query": q, "readMask": _READ_MASK,
                                   "pageSize": _MAX_RESULTS})
            if resp.status_code >= 300:
                worst = max(worst, resp.status_code)
                continue
            rows.extend(_entries(resp.json() or {}))
        return rows, worst

    try:
        rows, worst = _both(token)
        if worst in (401, 403):
            # Revoked (401) or a token minted before the contacts scopes were
            # granted (403): drop the cache, re-mint once, retry.
            google_client._drop_cached_token(org_id)
            fresh, ferr = google_client._access_token(org_id, force_refresh=True)
            if not ferr and fresh and fresh != token:
                rows, worst = _both(fresh)
    except Exception as e:  # noqa: BLE001 — distilled reason, never content
        return {"ok": False, "error": f"contacts request failed ({type(e).__name__})"}

    if not rows and worst == 403:
        return {"ok": False, "error": (
            "the Google connection is missing the contacts permission — "
            "reconnect Google in the dashboard to grant it"
        )}
    if not rows and worst >= 300:
        return {"ok": False, "error": f"contacts HTTP {worst}"}

    seen: set[str] = set()
    deduped: list[dict] = []
    for r in rows:
        key = r["email"].lower()
        if key not in seen:
            seen.add(key)
            deduped.append(r)
        if len(deduped) >= _MAX_RESULTS:
            break
    return {"ok": True, "results": deduped}


def warmup(org_id: str) -> None:
    """Prime Google's contact-search indexes (empty-query warmup request per
    the People API docs) so the first real mid-meeting lookup isn't served a
    stale empty set. Best-effort: any failure is swallowed."""
    try:
        token, err = google_client._access_token(org_id)
        if err:
            return
        for url in (_SEARCH_CONTACTS, _SEARCH_OTHER):
            try:
                _get(url, token, {"query": "", "readMask": _READ_MASK})
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
