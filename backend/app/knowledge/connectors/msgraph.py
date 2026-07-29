"""Microsoft Graph adapter for the SourceConnector contract.

The adapter owns the Graph JSON → canonical-record translation and nothing
else: HTTP (auth, retries at the wire level) lives in the transport it is
constructed with. Tests and the offline demo inject
``fake_graph.FakeGraphTransport``; production injects ``GraphHttpTransport``
(client-credentials, admin-consented — see
docs/company-brain/MSGRAPH-PERMISSIONS.md).

Checkpoints ARE Graph's ``@odata.nextLink``/``@odata.deltaLink`` URLs —
durable, opaque, resumable mid-crawl, replay-safe.
"""
from __future__ import annotations

from typing import Any

from .base import (
    ConnectorAuthRevoked,
    ConnectorItemGone,
    ConnectorThrottled,
    DeltaPage,
    RemoteItem,
    RemotePermission,
    RemotePrincipal,
    TransportError,
)

_BASE = "https://graph.microsoft.com/v1.0"


def _map_error(e: TransportError) -> Exception:
    if e.status == 429:
        return ConnectorThrottled(e.retry_after or 30.0)
    if e.status in (401, 403):
        return ConnectorAuthRevoked()
    if e.status in (404, 410):
        return ConnectorItemGone()
    return RuntimeError(f"graph HTTP {e.status}")


class GraphConnector:
    def __init__(self, transport, *, drives: list[str] | None = None,
                 base_url: str = ""):
        self._t = transport
        self._drives = [d for d in (drives or []) if d]
        self._base = (base_url or _BASE).rstrip("/")

    def _json(self, url: str) -> dict[str, Any]:
        try:
            return self._t.get_json(url)
        except TransportError as e:
            raise _map_error(e) from e

    # ── contract ──
    def list_resources(self) -> list[str]:
        if self._drives:  # the admin's scope allowlist always wins
            return [f"drive:{d}" for d in self._drives]
        data = self._json(f"{self._base}/drives")
        return [f"drive:{d['id']}" for d in data.get("value", [])]

    def delta(self, resource_key: str, checkpoint: str) -> DeltaPage:
        drive_id = resource_key.split(":", 1)[1]
        url = checkpoint or f"{self._base}/drives/{drive_id}/root/delta"
        data = self._json(url)
        items = [
            self._to_item(resource_key, raw)
            for raw in data.get("value", [])
        ]
        next_link = str(data.get("@odata.nextLink") or "")
        delta_link = str(data.get("@odata.deltaLink") or "")
        return DeltaPage(
            items=items,
            checkpoint=next_link or delta_link or checkpoint,
            done=bool(delta_link),
        )

    def fetch_content(self, item: RemoteItem) -> bytes:
        drive_id = item.resource_key.split(":", 1)[1]
        url = f"{self._base}/drives/{drive_id}/items/{item.external_id}/content"
        try:
            return self._t.get_bytes(url)
        except TransportError as e:
            raise _map_error(e) from e

    def fetch_permissions(
        self, resource_key: str, external_id: str
    ) -> list[RemotePermission]:
        drive_id = resource_key.split(":", 1)[1]
        data = self._json(
            f"{self._base}/drives/{drive_id}/items/{external_id}/permissions"
        )
        out: list[RemotePermission] = []
        for raw in data.get("value", []):
            perm = self._to_permission(raw)
            if perm is not None:
                out.append(perm)
        return out

    def fetch_principals(
        self,
    ) -> tuple[list[RemotePrincipal], list[tuple[str, str]]]:
        principals: list[RemotePrincipal] = []
        for u in self._json(f"{self._base}/users").get("value", []):
            principals.append(RemotePrincipal(
                external_id=str(u.get("id") or ""), kind="user",
                display=str(u.get("displayName") or ""),
                email=str(u.get("mail") or "").lower(),
            ))
        edges: list[tuple[str, str]] = []
        for g in self._json(f"{self._base}/groups").get("value", []):
            gid = str(g.get("id") or "")
            principals.append(RemotePrincipal(
                external_id=gid, kind="group",
                display=str(g.get("displayName") or ""),
            ))
            members = self._json(
                f"{self._base}/groups/{gid}/members"
            ).get("value", [])
            for m in members:
                mid = str(m.get("id") or "")
                if mid:
                    edges.append((gid, mid))
        return principals, edges

    # ── Graph JSON mapping ──
    @staticmethod
    def _to_item(resource_key: str, raw: dict[str, Any]) -> RemoteItem:
        parent = raw.get("parentReference") or {}
        modified = raw.get("lastModifiedDateTime") or 0.0
        if isinstance(modified, str):
            # ISO-8601 from real Graph; fall back to 0 rather than crash sync
            from datetime import datetime

            try:
                modified = datetime.fromisoformat(
                    modified.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                modified = 0.0
        return RemoteItem(
            external_id=str(raw.get("id") or ""),
            resource_key=resource_key,
            title=str(raw.get("name") or ""),
            web_url=str(raw.get("webUrl") or ""),
            mime=str((raw.get("file") or {}).get("mimeType") or ""),
            parent_ref=f"{resource_key}:{parent.get('path') or ''}",
            source_version=str(raw.get("cTag") or raw.get("eTag") or ""),
            modified_at=float(modified or 0.0),
            author=str(
                ((raw.get("createdBy") or {}).get("user") or {})
                .get("displayName") or ""
            ),
            size=int(raw.get("size") or 0),
            deleted="deleted" in raw,
            folder="folder" in raw,
        )

    @staticmethod
    def _to_permission(raw: dict[str, Any]) -> RemotePermission | None:
        inherited = str(((raw.get("inheritedFrom") or {}).get("id")) or "")
        roles = raw.get("roles") or ["read"]
        role = str(roles[0] if roles else "read")
        granted = raw.get("grantedToV2") or {}
        if "user" in granted:
            u = granted["user"]
            principal = RemotePrincipal(
                external_id=str(u.get("id") or ""), kind="user",
                display=str(u.get("displayName") or ""),
                email=str(u.get("email") or u.get("mail") or "").lower(),
            )
        elif "group" in granted or "siteGroup" in granted:
            g = granted.get("group") or granted.get("siteGroup") or {}
            principal = RemotePrincipal(
                external_id=str(g.get("id") or ""), kind="group",
                display=str(g.get("displayName") or ""),
            )
        elif raw.get("link"):
            scope = str((raw.get("link") or {}).get("scope") or "")
            if scope == "organization":
                principal = RemotePrincipal(external_id="tenant", kind="tenant")
            else:
                # anonymous / users links: recorded, but 'link' principals are
                # never auto-granted to anyone (default deny — CONTRACTS.md)
                principal = RemotePrincipal(
                    external_id=f"link:{scope or 'unknown'}", kind="link"
                )
        else:
            return None
        if not principal.external_id:
            return None
        return RemotePermission(
            principal=principal, role=role, inherited_from=inherited
        )


class GraphHttpTransport:
    """Production transport skeleton (client credentials). Deliberately NOT
    exercised by tests or the demo — the slice never calls a real tenant.
    Wire-level throttling maps to TransportError(429, retry_after) so the
    adapter/sync engine handle it identically to the fake."""

    def __init__(self, *, tenant_id: str, client_id: str, client_secret: str):
        if not (tenant_id and client_id and client_secret):
            raise RuntimeError("msgraph credentials not configured")
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = ""
        self._token_expiry = 0.0

    def _access_token(self) -> str:
        import time

        import httpx

        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = httpx.post(
            "https://login.microsoftonline.com/"
            f"{self._tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=20.0,
        )
        if resp.status_code != 200:
            raise TransportError(resp.status_code)
        payload = resp.json()
        self._token = str(payload.get("access_token") or "")
        self._token_expiry = time.time() + float(payload.get("expires_in") or 0)
        if not self._token:
            raise TransportError(401)
        return self._token

    def _request(self, url: str) -> "httpx.Response":  # noqa: F821
        import httpx

        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {self._access_token()}"},
            timeout=30.0,
        )
        if resp.status_code == 429:
            raise TransportError(
                429, retry_after=float(resp.headers.get("Retry-After") or 30)
            )
        if resp.status_code >= 400:
            raise TransportError(resp.status_code)
        return resp

    def get_json(self, url: str) -> dict[str, Any]:
        return self._request(url).json()

    def get_bytes(self, url: str) -> bytes:
        return self._request(url).content
