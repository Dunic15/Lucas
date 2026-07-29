"""A synthetic, network-free Microsoft Graph tenant — the deterministic
transport behind the msgraph connector (contract v5's fixture discipline).

Holds users, groups (nested membership), drives and driveItems with per-item
and inherited permissions, and produces real Graph-shaped JSON: paged
``/root/delta`` with ``@odata.nextLink``/``@odata.deltaLink``, ``/permissions``
with ``grantedToV2``/``link``/``inheritedFrom``, ``/users``, ``/groups`` and
``/groups/{id}/members``. Mutations bump a monotonic change serial so delta
cursors behave exactly like Graph's: an old cursor replays every change since
it, an issued page replays byte-identically, and a drained crawl hands back a
delta cursor.

Test controls: ``throttle_next(n, retry_after)`` (429), ``revoke()`` (401),
``expire_delta()`` (410 resyncRequired -> full reconciliation). All data here
is SYNTHETIC — never real names or PII (CONTEXT.md hard constraint 4).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

_HOST = "https://graph.fake"


class TransportError(Exception):
    """Non-2xx from a Graph transport. Carries no response body — Graph error
    bodies can echo request URLs with embedded tokens."""

    def __init__(self, status: int, *, retry_after: float = 0.0):
        super().__init__(f"HTTP {status}")
        self.status = int(status)
        self.retry_after = float(retry_after)


class FakeGraphTenant:
    def __init__(self, tenant_id: str = "synth-tenant", page_size: int = 2):
        self.tenant_id = tenant_id
        self.page_size = max(1, int(page_size))
        self._serial = 0
        self._delta_floor = 0  # tokens at/below this are expired (410)
        self.users: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, dict[str, Any]] = {}
        self.drives: dict[str, dict[str, dict[str, Any]]] = {}

    # ── fixture builders ──
    def _bump(self) -> int:
        self._serial += 1
        return self._serial

    def add_user(self, uid: str, display: str, email: str) -> None:
        self.users[uid] = {"id": uid, "displayName": display, "mail": email}

    def add_group(self, gid: str, display: str, members: list[str]) -> None:
        self.groups[gid] = {"id": gid, "displayName": display,
                            "members": list(members)}

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
            "modified": 1_780_000_000.0 + self._serial,
        }

    def update_content(self, drive_id: str, item_id: str,
                       content: bytes) -> None:
        item = self.drives[drive_id][item_id]
        item["content"] = content
        item["ctag"] += 1  # content change ⇒ new version tag
        item["serial"] = self._bump()
        item["modified"] = 1_780_000_000.0 + self._serial

    def set_permissions(self, drive_id: str, item_id: str,
                        permissions: list[dict[str, Any]]) -> None:
        """Permission-only change: the change serial moves (so the item shows
        up in delta) but the cTag does NOT — exactly Graph's behavior."""
        item = self.drives[drive_id][item_id]
        item["perms"] = list(permissions)
        item["serial"] = self._bump()

    def delete_item(self, drive_id: str, item_id: str) -> None:
        item = self.drives[drive_id][item_id]
        item["deleted"] = True
        item["serial"] = self._bump()

    def expire_delta(self) -> None:
        """Invalidate every issued delta token — the next delta call gets 410
        resyncRequired, forcing a full reconciliation."""
        self._delta_floor = self._serial

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
            "parentReference": {"driveId": drive_id,
                                "path": self._path(drive_id, item)},
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
        ""            -> start initial crawl
        init:O:H      -> initial crawl, offset O, snapshot high-water H
        delta:S       -> changes with serial > S
        delta:S:O:H   -> change replay continuation
        Pages are pure reads: replaying a token returns the same page."""
        items = self.drives[drive_id]
        if token and self._token_expired(token):
            raise TransportError(410)
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
                return {"value": value,
                        "@odata.nextLink": self._delta_url(drive_id, nxt)}
            return {"value": value,
                    "@odata.deltaLink": self._delta_url(drive_id,
                                                        f"delta:{high}")}
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
            return {"value": value,
                    "@odata.nextLink": self._delta_url(drive_id, nxt)}
        return {"value": value,
                "@odata.deltaLink": self._delta_url(drive_id, f"delta:{high}")}

    def _token_expired(self, token: str) -> bool:
        if not self._delta_floor or not token.startswith("delta:"):
            return False
        try:
            return int(token.split(":")[1]) <= self._delta_floor
        except (IndexError, ValueError):
            return False

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
            return {"value": [{"id": g["id"],
                               "displayName": g["displayName"]}
                              for g in t.groups.values()]}
        if len(parts) == 3 and parts[0] == "groups" and parts[2] == "members":
            group = t.groups.get(parts[1])
            if group is None:
                raise TransportError(404)
            out = []
            for mid in group["members"]:
                out.append({
                    "@odata.type": "#microsoft.graph.group"
                    if mid in t.groups else "#microsoft.graph.user",
                    "id": mid,
                })
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


def tenant_from_fixture(fixture: dict[str, Any]) -> FakeGraphTenant:
    """Build a tenant from a connector's ``config_json['fixture']`` — the DF
    convention for network-free connectors (see FakeGDriveConnector).

    Shape:
      {"users":  [{"id","display","email"}],
       "groups": [{"id","display","members":[id,...]}],
       "drives": {drive_id: {item_id: {name, body, mime?, parent?, folder?,
                                       permissions?: [graph permission json],
                                       grant_users?: [uid], grant_groups?: [gid],
                                       org_wide?: bool, permissions_hidden?: bool}}},
       "page_size": 2}
    """
    tenant = FakeGraphTenant(page_size=int(fixture.get("page_size") or 2))
    for u in fixture.get("users") or []:
        tenant.add_user(str(u["id"]), str(u.get("display") or ""),
                        str(u.get("email") or ""))
    for g in fixture.get("groups") or []:
        tenant.add_group(str(g["id"]), str(g.get("display") or ""),
                         [str(m) for m in (g.get("members") or [])])
    for drive_id, items in (fixture.get("drives") or {}).items():
        tenant.add_drive(str(drive_id))
        for item_id, meta in items.items():
            perms = list(meta.get("permissions") or [])
            for uid in meta.get("grant_users") or []:
                perms.append(FakeGraphTenant.perm_user(str(uid)))
            for gid in meta.get("grant_groups") or []:
                perms.append(FakeGraphTenant.perm_group(str(gid)))
            if meta.get("org_wide"):
                perms.append(FakeGraphTenant.perm_org_link())
            if meta.get("permissions_hidden"):
                perms = []  # unreadable permissions ⇒ connector says unknown
            tenant.put_item(
                str(drive_id), str(item_id), str(meta.get("name") or item_id),
                str(meta.get("body") or "").encode(),
                mime=str(meta.get("mime") or "text/markdown"),
                parent=str(meta.get("parent") or ""),
                folder=bool(meta.get("folder")),
                permissions=perms,
            )
            if meta.get("permissions_hidden"):
                tenant.drives[str(drive_id)][str(item_id)]["hidden"] = True
    return tenant


def synthetic_tenant() -> FakeGraphTenant:
    """The default demo fixture: one drive, a nested folder with inherited
    group permission, an org-wide doc, a user-private doc, and a nested
    group. Synthetic names only."""
    t = FakeGraphTenant()
    t.add_user("u-ada", "Ada Test", "ada@synthetic.example")
    t.add_user("u-bo", "Bo Test", "bo@synthetic.example")
    t.add_group("g-eng", "Engineering", ["u-ada"])
    t.add_group("g-all-eng", "All Engineering", ["g-eng"])
    t.add_drive("drive-1")
    t.put_item("drive-1", "folder-eng", "Engineering", folder=True,
               permissions=[t.perm_group("g-all-eng", "All Engineering")])
    t.put_item(
        "drive-1", "doc-runbook", "runbook.md",
        b"# Deploy runbook\n\nSynthetic steps for the synthetic service.\n",
        mime="text/markdown", parent="folder-eng",
    )
    t.put_item(
        "drive-1", "doc-handbook", "handbook.md",
        b"# Company handbook\n\nSynthetic policies for everyone.\n",
        mime="text/markdown", permissions=[t.perm_org_link()],
    )
    t.put_item(
        "drive-1", "doc-payroll", "payroll.md",
        b"# Payroll\n\nSynthetic numbers only Bo may see.\n",
        mime="text/markdown",
        permissions=[t.perm_user("u-bo", "Bo Test", "bo@synthetic.example")],
    )
    return t
