"""A fully synthetic, in-memory Microsoft Graph tenant — the key-free
transport behind the Company Brain vertical slice.

``FakeGraphTenant`` holds users, groups (with nested membership), drives and
driveItems (with per-item and inherited permissions), and produces real
Graph-shaped JSON: paged ``/root/delta`` responses with
``@odata.nextLink``/``@odata.deltaLink``, ``/permissions`` with
``grantedToV2``/``link``/``inheritedFrom`` facets, ``/users``, ``/groups``
and ``/groups/{id}/members``. Mutations (content edit, permission change,
delete) bump a monotonic change serial so delta cursors behave exactly like
Graph's: an old cursor replays every change since it, an issued page replays
byte-identically, and a drained crawl hands back a delta cursor.

``FakeGraphTransport`` speaks the transport seam the real HTTP transport
implements (`get_json`/`get_bytes`), plus two test controls:
``throttle_next(n, retry_after)`` makes the next n calls fail 429, and
``revoke()`` makes every call fail 401. All data here is SYNTHETIC — never
put real names/PII in fixtures (CONTEXT.md hard constraint 4).
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from .base import TransportError

_HOST = "https://graph.fake"


class FakeGraphTenant:
    def __init__(self, tenant_id: str = "synth-tenant", page_size: int = 2):
        self.tenant_id = tenant_id
        self.page_size = max(1, int(page_size))
        self._serial = 0
        self.users: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, dict[str, Any]] = {}
        # drive_id -> {item_id -> item}; item: name, content, mime, parent,
        # folder, perms, ctag, serial, deleted
        self.drives: dict[str, dict[str, dict[str, Any]]] = {}

    # ── fixture builders ──
    def _bump(self) -> int:
        self._serial += 1
        return self._serial

    def add_user(self, uid: str, display: str, email: str) -> None:
        self.users[uid] = {"id": uid, "displayName": display, "mail": email}

    def add_group(self, gid: str, display: str, members: list[str]) -> None:
        self.groups[gid] = {
            "id": gid, "displayName": display, "members": list(members),
        }

    def set_group_members(self, gid: str, members: list[str]) -> None:
        self.groups[gid]["members"] = list(members)
        self._bump()

    def add_drive(self, drive_id: str) -> None:
        self.drives[drive_id] = {}

    def put_item(
        self, drive_id: str, item_id: str, name: str, content: bytes = b"",
        *, mime: str = "text/plain", parent: str = "", folder: bool = False,
        permissions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.drives[drive_id][item_id] = {
            "id": item_id, "name": name, "content": content, "mime": mime,
            "parent": parent, "folder": folder,
            "perms": list(permissions or []),
            "ctag": 1, "serial": self._bump(), "deleted": False,
            "modified": time.time(),
        }

    def update_content(self, drive_id: str, item_id: str, content: bytes) -> None:
        item = self.drives[drive_id][item_id]
        item["content"] = content
        item["ctag"] += 1  # content change ⇒ new version tag
        item["serial"] = self._bump()
        item["modified"] = time.time()

    def set_permissions(
        self, drive_id: str, item_id: str, permissions: list[dict[str, Any]]
    ) -> None:
        """A permission-only change: the change serial moves (the item shows
        up in delta) but the cTag does NOT — exactly Graph's behavior."""
        item = self.drives[drive_id][item_id]
        item["perms"] = list(permissions)
        item["serial"] = self._bump()

    def delete_item(self, drive_id: str, item_id: str) -> None:
        item = self.drives[drive_id][item_id]
        item["deleted"] = True
        item["serial"] = self._bump()

    # ── permission fixture shorthands (Graph JSON shapes) ──
    @staticmethod
    def perm_user(uid: str, display: str = "", email: str = "") -> dict:
        return {"roles": ["read"], "grantedToV2": {"user": {
            "id": uid, "displayName": display, "email": email}}}

    @staticmethod
    def perm_group(gid: str, display: str = "") -> dict:
        return {"roles": ["read"], "grantedToV2": {"group": {
            "id": gid, "displayName": display}}}

    @staticmethod
    def perm_org_link() -> dict:
        return {"roles": ["read"], "link": {"scope": "organization"}}

    @staticmethod
    def perm_anon_link() -> dict:
        return {"roles": ["read"], "link": {"scope": "anonymous"}}

    # ── Graph JSON production ──
    def _item_json(self, drive_id: str, item: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": item["id"],
            "name": item["name"],
            "cTag": f'c:{item["ctag"]}',
            "eTag": f'e:{item["ctag"]}',
            "size": len(item["content"]),
            "lastModifiedDateTime": item["modified"],
            "webUrl": f"{_HOST}/{drive_id}/{item['id']}",
            "parentReference": {
                "driveId": drive_id,
                "path": self._path(drive_id, item),
            },
            "createdBy": {"user": {"displayName": "Synthetic Author"}},
        }
        if item["folder"]:
            out["folder"] = {"childCount": 0}
        else:
            out["file"] = {"mimeType": item["mime"]}
        if item["deleted"]:
            out["deleted"] = {"state": "deleted"}
        return out

    def _path(self, drive_id: str, item: dict[str, Any]) -> str:
        parts: list[str] = []
        cur = item
        seen = 0
        while cur.get("parent") and seen < 16:
            cur = self.drives[drive_id].get(cur["parent"]) or {}
            if not cur:
                break
            parts.append(str(cur.get("name") or ""))
            seen += 1
        return "/drive/root:/" + "/".join(reversed(parts))

    def delta_page(self, drive_id: str, token: str) -> dict[str, Any]:
        """One page of /root/delta. Token grammar (opaque to callers):
        ""            → start initial crawl
        init:O:H      → initial crawl, offset O, snapshot high-water H
        delta:S       → changes with serial > S
        delta:S:O:H   → change replay continuation
        Pages are pure reads — replaying a token returns the same page."""
        items = self.drives[drive_id]
        if not token or token.startswith("init:"):
            offset, high = 0, self._serial
            if token:
                _, o, h = token.split(":")
                offset, high = int(o), int(h)
            live = sorted(
                (i for i in items.values()
                 if not i["deleted"] and i["serial"] <= high),
                key=lambda i: i["id"],
            )
            page = live[offset:offset + self.page_size]
            value = [self._item_json(drive_id, i) for i in page]
            if offset + self.page_size < len(live):
                nxt = f"init:{offset + self.page_size}:{high}"
                return {"value": value, "@odata.nextLink": self._delta_url(
                    drive_id, nxt)}
            return {"value": value, "@odata.deltaLink": self._delta_url(
                drive_id, f"delta:{high}")}
        parts = token.split(":")
        since = int(parts[1])
        offset = int(parts[2]) if len(parts) > 2 else 0
        high = int(parts[3]) if len(parts) > 3 else self._serial
        changed = sorted(
            (i for i in items.values() if since < i["serial"] <= high),
            key=lambda i: (i["serial"], i["id"]),
        )
        page = changed[offset:offset + self.page_size]
        value = [self._item_json(drive_id, i) for i in page]
        if offset + self.page_size < len(changed):
            nxt = f"delta:{since}:{offset + self.page_size}:{high}"
            return {"value": value, "@odata.nextLink": self._delta_url(
                drive_id, nxt)}
        return {"value": value, "@odata.deltaLink": self._delta_url(
            drive_id, f"delta:{high}")}

    @staticmethod
    def _delta_url(drive_id: str, token: str) -> str:
        return f"{_HOST}/v1.0/drives/{drive_id}/root/delta?token={token}"

    def permissions_json(self, drive_id: str, item_id: str) -> dict[str, Any]:
        items = self.drives[drive_id]
        item = items.get(item_id)
        if item is None or item["deleted"]:
            raise TransportError(404)
        value = [dict(p) for p in item["perms"]]
        cur = item
        seen = 0
        while cur.get("parent") and seen < 16:
            parent = items.get(cur["parent"])
            if parent is None:
                break
            for p in parent["perms"]:
                inherited = dict(p)
                inherited["inheritedFrom"] = {"id": parent["id"]}
                value.append(inherited)
            cur = parent
            seen += 1
        return {"value": value}


class FakeGraphTransport:
    """Transport seam double: routes Graph URLs to the tenant above."""

    def __init__(self, tenant: FakeGraphTenant):
        self.tenant = tenant
        self.calls: list[str] = []
        self._throttle_left = 0
        self._throttle_retry_after = 1.0
        self._revoked = False

    # test controls
    def throttle_next(self, n: int = 1, retry_after: float = 1.0) -> None:
        self._throttle_left = int(n)
        self._throttle_retry_after = float(retry_after)

    def revoke(self) -> None:
        self._revoked = True

    # transport seam
    def _gate(self, url: str) -> None:
        self.calls.append(url)
        if self._revoked:
            raise TransportError(401)
        if self._throttle_left > 0:
            self._throttle_left -= 1
            raise TransportError(429, retry_after=self._throttle_retry_after)

    def get_json(self, url: str) -> dict[str, Any]:
        self._gate(url)
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p and p != "v1.0"]
        t = self.tenant
        if parts == ["drives"]:
            return {"value": [{"id": d} for d in sorted(t.drives)]}
        if parts == ["users"]:
            return {"value": list(t.users.values())}
        if parts == ["groups"]:
            return {"value": [
                {"id": g["id"], "displayName": g["displayName"]}
                for g in t.groups.values()
            ]}
        if len(parts) == 3 and parts[0] == "groups" and parts[2] == "members":
            group = t.groups.get(parts[1])
            if group is None:
                raise TransportError(404)
            out = []
            for mid in group["members"]:
                if mid in t.groups:
                    out.append({"@odata.type": "#microsoft.graph.group",
                                "id": mid})
                else:
                    out.append({"@odata.type": "#microsoft.graph.user",
                                "id": mid})
            return {"value": out}
        if (len(parts) == 4 and parts[0] == "drives" and parts[2] == "root"
                and parts[3] == "delta"):
            drive_id = parts[1]
            if drive_id not in t.drives:
                raise TransportError(404)
            token = (parse_qs(parsed.query).get("token") or [""])[0]
            return t.delta_page(drive_id, token)
        if (len(parts) == 5 and parts[0] == "drives" and parts[2] == "items"
                and parts[4] == "permissions"):
            return t.permissions_json(parts[1], parts[3])
        raise TransportError(404)

    def get_bytes(self, url: str) -> bytes:
        self._gate(url)
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p and p != "v1.0"]
        if (len(parts) == 5 and parts[0] == "drives" and parts[2] == "items"
                and parts[4] == "content"):
            item = self.tenant.drives.get(parts[1], {}).get(parts[3])
            if item is None or item["deleted"]:
                raise TransportError(404)
            return bytes(item["content"])
        raise TransportError(404)


def synthetic_tenant() -> FakeGraphTenant:
    """The default demo fixture: one drive, a nested folder with inherited
    permissions, one org-wide doc, users and a nested group. Synthetic names
    only."""
    t = FakeGraphTenant()
    t.add_user("u-ada", "Ada Test", "ada@synthetic.example")
    t.add_user("u-bo", "Bo Test", "bo@synthetic.example")
    t.add_group("g-eng", "Engineering", ["u-ada"])
    t.add_group("g-all-eng", "All Engineering", ["g-eng"])
    t.add_drive("drive-1")
    t.put_item(
        "drive-1", "folder-eng", "Engineering", folder=True,
        permissions=[t.perm_group("g-all-eng", "All Engineering")],
    )
    t.put_item(
        "drive-1", "doc-runbook", "runbook.md",
        b"# Deploy runbook\n\nSynthetic steps for the synthetic service.\n",
        mime="text/markdown", parent="folder-eng",
    )
    t.put_item(
        "drive-1", "doc-handbook", "handbook.md",
        b"# Company handbook\n\nSynthetic policies for everyone.\n",
        mime="text/markdown",
        permissions=[t.perm_org_link()],
    )
    t.put_item(
        "drive-1", "doc-payroll", "payroll.md",
        b"# Payroll\n\nSynthetic numbers only Bo may see.\n",
        mime="text/markdown",
        permissions=[t.perm_user("u-bo", "Bo Test", "bo@synthetic.example")],
    )
    return t
