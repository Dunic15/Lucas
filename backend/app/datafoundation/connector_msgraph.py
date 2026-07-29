"""Microsoft Graph DF connector — SharePoint/OneDrive items + Entra ACLs.

Speaks the real Graph protocols (driveItem ``/root/delta`` with
``@odata.nextLink``/``@odata.deltaLink``, per-item ``/permissions``,
``/users``+``/groups``+members) and translates them into DF SourceEnvelopes.
It owns NO storage and NO ACL logic of its own: ``dal.commit_batch`` applies
everything atomically and ``visible_heads`` remains the single visibility
query. Transport is injected, so the whole path runs network-free.

Protocol behaviors the contract cares about:

- **Delta tokens are the cursor.** The committed cursor holds one opaque
  token per drive; ``commit_batch`` advances it in the same transaction as
  the envelopes, so a failed batch never loses changes.
- **410 ``resyncRequired`` -> full reconciliation** (never a modified-time
  watermark, which the contract forbids).
- **429 -> bounded in-connector backoff** honoring ``Retry-After``; beyond the
  budget the run retries on DF's own ladder.
- **401/403 -> ``ScopeLostError``**, so the connector leaves eligibility and
  every mirrored record it owns drops out of visibility immediately.
- **Unreadable permissions -> ``acl_mode='unknown'``** (zero ACL rows: visible
  to nobody), the same fail-closed move as gdrive's ``permissions_hidden``.

Real-tenant HTTP is credential-gated and deliberately unreachable here: with
no fixture and no injected transport the connector raises
``ConnectorNotImplemented``, exactly like the fake Drive connector.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from . import dal
from .connectors import ConnectorNotImplemented, SyncBatch
from .envelope import body_checksum
from .fake_graph import FakeGraphTransport, TransportError, tenant_from_fixture

_BASE = "https://graph.fake/v1.0"

# Bounded in-connector throttle budget: Graph's Retry-After is authoritative,
# but a connector must never sleep unboundedly inside a leased run.
_MAX_THROTTLE_RETRIES = 3
_MAX_THROTTLE_SLEEP = 5.0
# Hard page cap per run: the committed cursor makes the next run continue.
_MAX_PAGES_PER_RUN = 50

# Test/injection seam: (org_id, connector) -> transport. Production would
# install a credentialed HTTP transport here; tests install a fake tenant.
_TRANSPORT_FACTORY: Optional[Callable[[str, dict], Any]] = None


def set_transport_factory(factory: Optional[Callable[[str, dict], Any]]) -> None:
    global _TRANSPORT_FACTORY
    _TRANSPORT_FACTORY = factory


class GraphThrottled(RuntimeError):
    """Throttle budget exhausted — DF retries the run on its own ladder."""


def _transport(org_id: str, connector: dict) -> Any:
    if _TRANSPORT_FACTORY is not None:
        transport = _TRANSPORT_FACTORY(org_id, connector)
        if transport is not None:
            return transport
    fixture = (connector.get("config_json") or {}).get("fixture")
    if isinstance(fixture, dict):
        return FakeGraphTransport(tenant_from_fixture(fixture))
    raise ConnectorNotImplemented(
        "msgraph HTTP client is credential-gated and deferred; this "
        "connector runs only with a network-free fixture or an injected "
        "transport"
    )


class MSGraphConnector:
    kind = "msgraph"
    # Authoritative ACL mirror: sync marks the connector acl_mirrored after a
    # successful run, which is what lets its mirrored records become visible.
    mirrors_acl = True

    def sync(self, org_id: str, connector: dict, cursor: dict,
             *, full: bool) -> SyncBatch:
        transport = _transport(org_id, connector)
        config = connector.get("config_json") or {}
        scoped = [str(d) for d in (config.get("drives") or []) if d]

        identities, edges = self._principals(transport)
        for ident in identities:
            ident["members"] = edges.get(ident["external_id"], [])

        drives = scoped or self._drive_ids(transport)
        tokens = dict((cursor or {}).get("delta") or {})
        envelopes: list[dict] = []
        pages = 0
        for drive_id in drives:
            token = "" if full else str(tokens.get(drive_id) or "")
            while pages < _MAX_PAGES_PER_RUN:
                try:
                    page = self._delta_page(transport, drive_id, token)
                except TransportError as exc:
                    if exc.status in (410,):
                        # Token expired: restart this drive from scratch. The
                        # contract's only legal fallback — never a watermark.
                        token = ""
                        continue
                    raise self._map_error(exc) from exc
                pages += 1
                for raw in page.get("value", []):
                    env = self._envelope(transport, drive_id, raw)
                    if env is not None:
                        envelopes.append(env)
                next_link = str(page.get("@odata.nextLink") or "")
                delta_link = str(page.get("@odata.deltaLink") or "")
                token = self._token_of(next_link or delta_link)
                if delta_link or not next_link:
                    break
            tokens[drive_id] = token
        return SyncBatch(envelopes, identities=identities,
                         new_cursor={"delta": tokens})

    # ── Graph calls (throttle-aware) ──
    def _get_json(self, transport, url: str) -> dict:
        attempt = 0
        while True:
            try:
                return transport.get_json(url)
            except TransportError as exc:
                if exc.status == 429 and attempt < _MAX_THROTTLE_RETRIES:
                    time.sleep(min(max(exc.retry_after, 0.0),
                                   _MAX_THROTTLE_SLEEP))
                    attempt += 1
                    continue
                raise

    def _get_bytes(self, transport, url: str) -> bytes:
        attempt = 0
        while True:
            try:
                return transport.get_bytes(url)
            except TransportError as exc:
                if exc.status == 429 and attempt < _MAX_THROTTLE_RETRIES:
                    time.sleep(min(max(exc.retry_after, 0.0),
                                   _MAX_THROTTLE_SLEEP))
                    attempt += 1
                    continue
                raise

    @staticmethod
    def _map_error(exc: TransportError) -> Exception:
        if exc.status in (401, 403):
            return dal.ScopeLostError("graph consent or scope lost")
        if exc.status == 429:
            return GraphThrottled("graph throttle budget exhausted")
        return RuntimeError(f"graph HTTP {exc.status}")

    def _drive_ids(self, transport) -> list[str]:
        data = self._get_json(transport, f"{_BASE}/drives")
        return [str(d["id"]) for d in data.get("value", [])]

    def _delta_page(self, transport, drive_id: str, token: str) -> dict:
        url = f"{_BASE}/drives/{drive_id}/root/delta"
        if token:
            url = f"{url}?token={token}"
        try:
            return self._get_json(transport, url)
        except TransportError as exc:
            if exc.status == 410:
                raise
            raise self._map_error(exc) from exc

    @staticmethod
    def _token_of(link: str) -> str:
        if not link or "token=" not in link:
            return ""
        return link.split("token=", 1)[1].split("&", 1)[0]

    def _principals(self, transport) -> tuple[list[dict], dict[str, list[str]]]:
        identities: list[dict] = []
        edges: dict[str, list[str]] = {}
        try:
            users = self._get_json(transport, f"{_BASE}/users")
            groups = self._get_json(transport, f"{_BASE}/groups")
        except TransportError as exc:
            raise self._map_error(exc) from exc
        for u in users.get("value", []):
            email = str(u.get("mail") or "").lower()
            identities.append({
                "external_id": str(u.get("id") or ""), "kind": "user",
                "display": str(u.get("displayName") or ""), "email": email,
                # Entra is an authoritative issuer for its own tenant's
                # mailboxes; the connector's trusted_email_issuer flag is what
                # actually decides whether DF auto-binds on it.
                "email_verified": bool(email),
            })
        for g in groups.get("value", []):
            gid = str(g.get("id") or "")
            identities.append({
                "external_id": gid, "kind": "group",
                "display": str(g.get("displayName") or ""),
            })
            try:
                members = self._get_json(
                    transport, f"{_BASE}/groups/{gid}/members"
                )
            except TransportError as exc:
                raise self._map_error(exc) from exc
            edges[gid] = [str(m.get("id") or "")
                          for m in members.get("value", []) if m.get("id")]
        return identities, edges

    # ── Graph JSON -> SourceEnvelope ──
    def _envelope(self, transport, drive_id: str,
                  raw: dict[str, Any]) -> dict[str, Any] | None:
        external_id = str(raw.get("id") or "")
        if not external_id:
            return None
        deleted = "deleted" in raw
        title = str(raw.get("name") or "")
        parent = raw.get("parentReference") or {}
        if deleted:
            return {
                "external_id": external_id, "kind": "document", "title": title,
                "acl_mode": "unknown", "acl": [], "deleted": True,
                "checksum": "", "transform": "msgraph@1",
                "canonical_url": str(raw.get("webUrl") or ""),
                "container_external_id": f"drive:{drive_id}",
            }
        if "folder" in raw:
            # Containers carry inheritance, not content: their permissions are
            # materialized onto children by fetch, so no record is emitted.
            return None
        acl_mode, acl = self._acl(transport, drive_id, external_id)
        body = ""
        if acl_mode != "unknown":
            # An unreadable-ACL item is invisible anyway; skip the body fetch
            # rather than pull content we would never be allowed to serve.
            try:
                body = self._get_bytes(
                    transport,
                    f"{_BASE}/drives/{drive_id}/items/{external_id}/content",
                ).decode("utf-8", errors="replace")
            except TransportError as exc:
                if exc.status in (404, 410):
                    body = ""
                else:
                    raise self._map_error(exc) from exc
        return {
            "external_id": external_id,
            "kind": "document",
            "title": title,
            "body_text": body or None,
            "mime": str((raw.get("file") or {}).get("mimeType") or ""),
            "canonical_url": str(raw.get("webUrl") or ""),
            "author_external_id": str(
                ((raw.get("createdBy") or {}).get("user") or {})
                .get("displayName") or ""
            ),
            "container_external_id": str(
                parent.get("path") or f"drive:{drive_id}"
            ),
            "external_updated_at": self._iso(raw.get("lastModifiedDateTime")),
            "acl_mode": acl_mode,
            "acl": acl,
            "deleted": False,
            "checksum": str(raw.get("cTag") or raw.get("eTag") or "")
            or body_checksum(body),
            "transform": "msgraph@1",
        }

    def _acl(self, transport, drive_id: str,
             item_id: str) -> tuple[str, list[dict]]:
        """(acl_mode, acl entries). Fail-closed: anything we cannot read or
        understand resolves to 'unknown', which stores zero ACL rows."""
        try:
            data = self._get_json(
                transport,
                f"{_BASE}/drives/{drive_id}/items/{item_id}/permissions",
            )
        except TransportError as exc:
            if exc.status in (401, 403):
                raise self._map_error(exc) from exc
            return "unknown", []
        entries: list[dict] = []
        org_wide = False
        for perm in data.get("value", []):
            granted = perm.get("grantedToV2") or {}
            roles = perm.get("roles") or ["read"]
            access = "writer" if "write" in str(roles).lower() else "reader"
            if "user" in granted:
                ext = str((granted["user"] or {}).get("id") or "")
                kind = "user"
            elif "group" in granted or "siteGroup" in granted:
                node = granted.get("group") or granted.get("siteGroup") or {}
                ext = str(node.get("id") or "")
                kind = "group"
            elif perm.get("link"):
                scope = str((perm.get("link") or {}).get("scope") or "")
                if scope == "organization":
                    org_wide = True
                # anonymous/public links never grant retrieval visibility
                continue
            else:
                continue
            if ext:
                entries.append({"principal_kind": kind,
                                "principal_external_id": ext,
                                "access": access})
        if org_wide:
            return "org_default", []
        if entries:
            return "mirrored", entries
        return "unknown", []

    @staticmethod
    def _iso(value: Any) -> str:
        if not value:
            return ""
        if isinstance(value, str):
            return value[:40]
        try:
            from datetime import datetime, timezone

            return datetime.fromtimestamp(
                float(value), tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError, OSError):
            return ""
