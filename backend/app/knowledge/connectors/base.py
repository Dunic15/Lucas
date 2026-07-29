"""The source-agnostic connector contract (docs/company-brain/CONTRACTS.md).

A connector turns one external source (a Graph tenant, a Drive domain, a
Slack workspace) into a stream of canonical records the sync engine can
apply. The engine relies on exactly five behaviors:

1. ``delta(resource_key, checkpoint)`` with ``checkpoint=""`` starts the
   initial crawl; the returned checkpoint resumes exactly after the last
   applied page, and replaying a page is safe (the engine upserts by
   ``external_id`` + ``source_version``).
2. Once ``done=True``, the checkpoint is a delta cursor: later calls return
   only changes — including deletions (``deleted=True`` items) and
   permission-only changes (same ``source_version``, new ACLs).
3. ``ConnectorThrottled`` is not a failure: the engine saves state and
   reschedules honoring ``retry_after``.
4. ``ConnectorAuthRevoked`` flips the connection to ``revoked``; the engine
   never retries past it, and retrieval stops serving the source immediately.
5. Checkpoints are opaque strings; the engine stores and echoes them.

Connectors never see Laura's stores and never log document content.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ConnectorThrottled(Exception):
    """The source asked us to back off; retry_after is seconds."""

    def __init__(self, retry_after: float = 30.0):
        super().__init__(f"throttled ({retry_after}s)")
        self.retry_after = max(1.0, float(retry_after))


class ConnectorAuthRevoked(Exception):
    """Credentials/consent no longer valid — a state change, not an error."""


class ConnectorItemGone(Exception):
    """The item vanished between the delta listing and the fetch."""


class TransportError(Exception):
    """Raised by transports for non-2xx responses; adapters map it to the
    typed exceptions above. Carries no response body (bodies can echo URLs
    with embedded tokens)."""

    def __init__(self, status: int, *, retry_after: float = 0.0):
        super().__init__(f"HTTP {status}")
        self.status = int(status)
        self.retry_after = float(retry_after)


@dataclass
class RemotePrincipal:
    external_id: str
    kind: str  # user | group | domain | tenant | everyone | link
    display: str = ""
    email: str = ""


@dataclass
class RemotePermission:
    principal: RemotePrincipal
    role: str = "read"
    inherited_from: str = ""  # "" = direct; else the container's external id


@dataclass
class RemoteItem:
    external_id: str
    resource_key: str
    title: str = ""
    web_url: str = ""
    mime: str = ""
    parent_ref: str = ""
    source_version: str = ""  # eTag/cTag equivalent; "" = unknown ⇒ re-ingest
    modified_at: float = 0.0  # epoch seconds; 0 = unknown
    author: str = ""
    size: int = 0
    deleted: bool = False
    folder: bool = False


@dataclass
class DeltaPage:
    items: list[RemoteItem] = field(default_factory=list)
    checkpoint: str = ""
    done: bool = False


class SourceConnector(Protocol):
    def list_resources(self) -> list[str]:
        """The scoped containers to sync (admin allowlist wins over 'all')."""
        ...

    def delta(self, resource_key: str, checkpoint: str) -> DeltaPage:
        ...

    def fetch_content(self, item: RemoteItem) -> bytes:
        ...

    def fetch_permissions(
        self, resource_key: str, external_id: str
    ) -> list[RemotePermission]:
        ...

    def fetch_principals(
        self,
    ) -> tuple[list[RemotePrincipal], list[tuple[str, str]]]:
        """(all directory principals, direct membership edges as
        (group_external_id, member_external_id))."""
        ...
