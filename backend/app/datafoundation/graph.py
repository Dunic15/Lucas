"""Microsoft Graph — Data Foundation connector #1 (M365 / Entra ID).

This is the first *enterprise* connector: it mirrors SharePoint / OneDrive
documents together with their real permissions and the Entra ID identity graph
(users, groups, nested membership). It slots into the accepted DF v5 contract
without new schema — it emits SourceEnvelopes + identities + a new cursor;
``dal.commit_batch`` applies everything atomically and the existing resolver
enforces the one visibility rule. What is genuinely new lives here:

  * The **connector contract** as five explicit transport operations —
    ``list`` / ``delta`` / ``fetch_content`` / ``fetch_permissions`` /
    ``fetch_principals`` — behind an INJECTED ``GraphTransport`` so the real
    HTTP client and a deterministic fake are the same shape.
  * A **deterministic fake Graph tenant** (``FakeGraphTransport``) built from a
    fixture held in the connector's ``config_json['graph_fixture']`` — zero
    network, exactly like the fake gdrive connector. The real credential-gated
    HTTP transport is deferred; a connector without a fixture raises
    ``ConnectorNotImplemented`` (legal rows, a failed run — never a silent
    org_default leak).
  * Incremental sync over the real Graph **delta** protocol: paged enumeration
    (``@odata.nextLink``), a **delta checkpoint** token (``@odata.deltaLink``),
    delta-expiry → full reconciliation, ``@removed`` **tombstones**, HTTP
    **429 throttling** → reschedule (the batch never commits, the cursor never
    advances), and **401 / scope loss** → the connector leaves eligibility.

Fail-closed throughout: an item whose permissions can't be read is
``acl_mode='unknown'`` (visible to nobody). Document bodies are UNTRUSTED
CONTENT — chunked and cited downstream, never instructions. Nothing here logs
bodies, emails, or tokens.
"""
from __future__ import annotations

from typing import Any

from . import dal
from .connectors import ConnectorNotImplemented, SyncBatch
from .envelope import body_checksum

# Drain guards: a runaway fixture (or a real tenant paging pathologically)
# must never spin forever inside one sync tick.
_MAX_PAGES = 500
_MAX_ITEMS = 20_000


class GraphThrottled(RuntimeError):
    """HTTP 429 from Graph. The run reschedules; the cursor never advances."""

    def __init__(self, message: str = "graph throttled", retry_after: int = 0):
        super().__init__(message)
        self.retry_after = int(retry_after or 0)


class GraphTransport:
    """The five operations a Graph sync needs. The real HTTP client and the
    fake tenant implement exactly this surface, so the connector never knows
    which one it holds (dependency injection)."""

    def fetch_principals(self) -> list[dict]:
        """Entra ID identities: users + groups (with member external ids, which
        may themselves be groups → nested membership)."""
        raise NotImplementedError

    def list_items(self, page_token: str) -> dict:
        """One page of a FULL enumeration.
        Returns ``{"items": [...], "next": page_token, "delta": token}`` —
        ``next`` empty on the last page, where ``delta`` is the checkpoint for
        future incrementals."""
        raise NotImplementedError

    def delta(self, delta_token: str) -> dict:
        """One page of INCREMENTAL changes for a checkpoint.
        Returns ``{"changes": [...], "next": page_token, "delta": token,
        "expired": bool}``. ``expired`` true ⇒ the token is stale and the
        caller must fall back to a full enumeration (contract: never a
        modified-time watermark)."""
        raise NotImplementedError

    def fetch_content(self, item: dict) -> str:
        """The item's extracted body text ("" for a structured/no-body item)."""
        raise NotImplementedError

    def fetch_permissions(self, item: dict) -> dict:
        """The item's EFFECTIVE permissions (inheritance already resolved).
        Returns ``{"readable": bool, "org_wide": bool, "acl": [...]}``; each acl
        entry is ``{principal_kind, principal_external_id, access}``. Not
        readable ⇒ the record is stored fail-closed (visible to nobody)."""
        raise NotImplementedError


class FakeGraphTransport(GraphTransport):
    """A deterministic, network-free Graph tenant driven entirely by a fixture.

    Fixture shape (connector.config_json['graph_fixture'])::

        {
          "items": {item_id: {"name", "body", "container", "author",
                              "mime", "web_url", "updated_at",
                              "acl": [{principal_kind, principal_external_id,
                                       access}],
                              "org_wide": bool,
                              "permissions_readable": bool,   # default true
                              "inherits_from": container_id}},  # inherited ACL
          "containers": {container_id: {"acl": [...], "org_wide": bool}},
          "principals": {
             "users":  [{"id", "display", "email", "email_verified"}],
             "groups": [{"id", "display", "members": [id, ...]}]},
          "deltas": [{"token": int, "item": id, "op": "upsert"|"remove",
                      ...item overrides}],
          "start_delta_token": int,          # default 1
          "latest_delta_token": int,         # default = max delta token
          "page_size": int,                  # default 50 (paging granularity)
          "throttle_on_page": int | null,    # raise 429 serving this page index
          "auth_lost": bool,                 # raise 401 / scope loss
          "expire_tokens_below": int,        # delta token < this ⇒ expired
        }

    The fixture is IMMUTABLE input; the transport never mutates it.
    """

    def __init__(self, fixture: dict):
        self._fx = fixture or {}
        self._page_size = max(1, int(self._fx.get("page_size") or 50))
        self._throttle_page = self._fx.get("throttle_on_page")
        # A full enumeration reports the BASELINE token ("you are caught up to
        # here"); future changes in ``deltas`` carry strictly larger tokens and
        # a delta drain advances the cursor to the LATEST of them.
        self._baseline = int(
            self._fx.get("baseline_token")
            or self._fx.get("start_delta_token") or 1
        )
        self._latest = max(
            self._baseline,
            int(self._fx.get("latest_delta_token") or 0),
            max((int(c.get("token") or 0)
                 for c in self._fx.get("deltas") or []), default=0),
        )

    # ── principals ──────────────────────────────────────────────────────────
    def fetch_principals(self) -> list[dict]:
        self._auth_check()
        principals = self._fx.get("principals") or {}
        out: list[dict] = []
        for user in principals.get("users") or []:
            out.append({
                "external_id": str(user.get("id") or ""),
                "kind": "user",
                "display": str(user.get("display") or ""),
                "email": str(user.get("email") or ""),
                "email_verified": bool(user.get("email_verified")),
            })
        for group in principals.get("groups") or []:
            out.append({
                "external_id": str(group.get("id") or ""),
                "kind": "group",
                "display": str(group.get("display") or ""),
                "members": [str(m) for m in (group.get("members") or [])],
            })
        return [p for p in out if p["external_id"]]

    # ── full enumeration (paged) ─────────────────────────────────────────────
    def list_items(self, page_token: str) -> dict:
        self._auth_check()
        ids = sorted((self._fx.get("items") or {}).keys())
        _base, offset = self._decode_cont(page_token)
        self._throttle_check(offset // self._page_size)
        window = ids[offset:offset + self._page_size]
        next_offset = offset + self._page_size
        more = next_offset < len(ids)
        return {
            "items": [self._item(i, {}) for i in window],
            "next": self._encode_cont(self._baseline, next_offset) if more
            else "",
            "delta": "" if more else f"d{self._baseline}",
        }

    # ── incremental (paged, checkpointed) ────────────────────────────────────
    def delta(self, delta_token: str) -> dict:
        self._auth_check()
        base, offset = self._decode_cont(delta_token)
        if base < int(self._fx.get("expire_tokens_below") or 0):
            return {"changes": [], "next": "", "delta": "", "expired": True}
        changes = sorted(
            (c for c in self._fx.get("deltas") or []
             if int(c.get("token") or 0) > base),
            key=lambda c: int(c.get("token") or 0),
        )
        # Page the change stream the way Graph does (a nextLink chain, then the
        # deltaLink). The continuation token carries BOTH the delta base and the
        # offset so a mid-drain 429 resumes deterministically against the same
        # window.
        self._throttle_check(offset // self._page_size)
        window = changes[offset:offset + self._page_size]
        next_offset = offset + self._page_size
        more = next_offset < len(changes)
        out_changes = []
        for change in window:
            item_id = str(change.get("item") or "")
            if str(change.get("op") or "") == "remove":
                out_changes.append({"id": item_id, "removed": True})
            else:
                overrides = {k: v for k, v in change.items()
                             if k not in ("token", "item", "op")}
                out_changes.append(self._item(item_id, overrides))
        return {
            "changes": out_changes,
            "next": self._encode_cont(base, next_offset) if more else "",
            "delta": "" if more else f"d{self._latest}",
            "expired": False,
        }

    # ── content + permissions ────────────────────────────────────────────────
    def fetch_content(self, item: dict) -> str:
        return str(item.get("body") or "")

    def fetch_permissions(self, item: dict) -> dict:
        self._auth_check()
        if item.get("permissions_readable") is False:
            return {"readable": False, "org_wide": False, "acl": []}
        acl = [dict(e) for e in (item.get("acl") or [])]
        org_wide = bool(item.get("org_wide"))
        # SharePoint/OneDrive inheritance: a document inherits its container's
        # (site/folder) grants unless it breaks inheritance. The transport
        # resolves the EFFECTIVE set the connector stores.
        parent = str(item.get("inherits_from") or "")
        if parent:
            container = (self._fx.get("containers") or {}).get(parent) or {}
            acl.extend(dict(e) for e in (container.get("acl") or []))
            org_wide = org_wide or bool(container.get("org_wide"))
        return {"readable": True, "org_wide": org_wide, "acl": acl}

    # ── fixture helpers ──────────────────────────────────────────────────────
    def _item(self, item_id: str, overrides: dict) -> dict:
        base = dict((self._fx.get("items") or {}).get(item_id) or {})
        base.update(overrides)
        base["_id"] = item_id
        return base

    def _auth_check(self) -> None:
        if self._fx.get("auth_lost"):
            raise dal.ScopeLostError("graph auth/permission scope lost (401)")

    def _throttle_check(self, page_index: int) -> None:
        if self._throttle_page is not None and int(self._throttle_page) == int(
            page_index
        ):
            raise GraphThrottled("graph 429 on page", retry_after=1)

    @staticmethod
    def _encode_cont(base: int, offset: int) -> str:
        """A page-continuation token that carries the delta base + offset."""
        return f"p{base}:{offset}"

    @staticmethod
    def _decode_cont(token: str) -> tuple[int, int]:
        """(delta_base, offset). ``d<n>`` is a fresh checkpoint at offset 0;
        ``p<base>:<offset>`` is a mid-drain continuation; anything else is a
        cold start (base 0, offset 0)."""
        t = str(token or "")
        try:
            if t.startswith("d"):
                return int(t[1:] or 0), 0
            if t.startswith("p"):
                base, _, off = t[1:].partition(":")
                return int(base or 0), int(off or 0)
        except ValueError:
            return 0, 0
        return 0, 0


class GraphConnector:
    """Drives a ``GraphTransport`` into a DF SyncBatch. Pure orchestration —
    it touches no database (``dal.commit_batch`` owns persistence)."""

    kind = "graph"

    def _transport(self, connector: dict) -> GraphTransport:
        fixture = (connector.get("config_json") or {}).get("graph_fixture")
        if isinstance(fixture, dict):
            return FakeGraphTransport(fixture)
        raise ConnectorNotImplemented(
            "graph HTTP transport is credential-gated and deferred; this "
            "connector runs today only with a network-free fixture"
        )

    def sync(self, org_id: str, connector: dict, cursor: dict,
             *, full: bool) -> SyncBatch:
        transport = self._transport(connector)
        identities = transport.fetch_principals()
        delta_token = str((cursor or {}).get("delta_token") or "")
        if full or not delta_token:
            envelopes, new_token = self._full(transport)
        else:
            result = self._incremental(transport, delta_token)
            if result is None:  # delta expired → full reconciliation
                envelopes, new_token = self._full(transport)
            else:
                envelopes, new_token = result
        return SyncBatch(
            envelopes, identities=identities,
            new_cursor={"delta_token": new_token},
        )

    def _full(self, transport: GraphTransport) -> tuple[list[dict], str]:
        envelopes: list[dict] = []
        page_token = ""
        delta_token = ""
        for _ in range(_MAX_PAGES):
            page = transport.list_items(page_token)
            for raw in page.get("items") or []:
                envelopes.append(self._envelope(transport, raw))
                if len(envelopes) >= _MAX_ITEMS:
                    break
            page_token = str(page.get("next") or "")
            delta_token = str(page.get("delta") or "") or delta_token
            if not page_token or len(envelopes) >= _MAX_ITEMS:
                break
        return envelopes, delta_token

    def _incremental(self, transport: GraphTransport,
                     delta_token: str) -> tuple[list[dict], str] | None:
        envelopes: list[dict] = []
        token = delta_token
        new_token = ""
        for _ in range(_MAX_PAGES):
            page = transport.delta(token)
            if page.get("expired"):
                return None
            for raw in page.get("changes") or []:
                if raw.get("removed"):
                    envelopes.append(self._tombstone(str(raw.get("id") or "")))
                else:
                    envelopes.append(self._envelope(transport, raw))
                if len(envelopes) >= _MAX_ITEMS:
                    break
            next_token = str(page.get("next") or "")
            new_token = str(page.get("delta") or "") or new_token
            if not next_token or len(envelopes) >= _MAX_ITEMS:
                break
            token = next_token
        return envelopes, new_token or delta_token

    def _envelope(self, transport: GraphTransport, raw: dict) -> dict:
        item_id = str(raw.get("_id") or raw.get("id") or "")
        body = transport.fetch_content(raw)
        perms = transport.fetch_permissions(raw)
        if not perms.get("readable"):
            acl_mode, acl = "unknown", []
        elif perms.get("org_wide"):
            acl_mode, acl = "org_default", []
        else:
            acl = [
                {
                    "principal_kind": ("group" if str(
                        e.get("principal_kind")) == "group" else "user"),
                    "principal_external_id": str(
                        e.get("principal_external_id") or ""),
                    "access": str(e.get("access") or "reader"),
                }
                for e in (perms.get("acl") or [])
                if str(e.get("principal_external_id") or "")
            ]
            # No permission rows the connector could read ⇒ nobody, not
            # everybody (fail-closed; validate_envelope also enforces this).
            acl_mode = "mirrored" if acl else "unknown"
        return {
            "external_id": item_id,
            "kind": "document",
            "title": str(raw.get("name") or item_id),
            "body_text": body,
            "mime": str(raw.get("mime") or "text/plain"),
            "canonical_url": str(raw.get("web_url") or ""),
            "author_external_id": str(raw.get("author") or ""),
            "container_external_id": str(raw.get("container") or ""),
            "external_updated_at": str(raw.get("updated_at") or ""),
            "acl_mode": acl_mode,
            "acl": acl,
            "deleted": False,
            "checksum": body_checksum(body),
            "transform": "graph@1",
        }

    @staticmethod
    def _tombstone(item_id: str) -> dict:
        return {
            "external_id": item_id,
            "kind": "document",
            "title": item_id,
            "external_updated_at": "",
            "acl_mode": "unknown",
            "acl": [],
            "deleted": True,
            "checksum": body_checksum(""),
            "transform": "graph@1",
        }


_CONNECTOR: Any = GraphConnector()


def connector() -> GraphConnector:
    return _CONNECTOR
