"""In-process Northstar browser provider (BrowserProvider) — deterministic.

Drives the Northstar synthetic product WITHOUT a real browser, a network
socket, or model credentials: it reads the product's own state
(``demos.northstar.product.store``) and renders each allowed URL into a
BrowserObservation. This is what lets the whole MVP journey run key-free and
deterministically while still exercising the REAL product write (the guarded
create mutates the product store; a re-observation sees task-0003).

It is a legitimate provider behind the B0/B1 interface: policy classification,
coordinate grounding, the canonical approval and the receipt all operate
unchanged. Screenshot "bytes" are a deterministic JSON perception payload (the
visual-only cue a vision model would see) — transient, stripped at the operator
boundary, never persisted/logged. No page-controlled ids leak: element ids are
server-defined.
"""
from __future__ import annotations

import json
import threading

from ..browser.provider import ProviderError, ProviderSession, RawObservation

_VIEWPORT = {"width": 1280, "height": 720}
_HOME = "http://127.0.0.1:8971/"

# Deterministic geometry for the five onboarding stage nodes: all share the
# accessible name "Onboarding stage" and carry NO stage-name text — the blocked
# one is knowable only from fill (amber) + position (2nd), i.e. visually.
_STAGE_ORDER = ["kickoff", "integration", "config", "uat", "golive"]
_STAGE_FILL = {"kickoff": "green", "integration": "amber", "config": "grey",
               "uat": "grey", "golive": "grey"}
_STAGE_TARGET = "integration"  # the blocked stage (seed VISUAL_TARGET_STAGE_ID)


def _stage_bbox(order: int) -> list[int]:
    cx, cy, r = 120 + order * 200, 140, 44
    return [cx - r, cy - r, cx + r, cy + r]


def _stage_elements() -> list[dict]:
    els = []
    for order, slug in enumerate(_STAGE_ORDER):
        els.append({
            "id": f"stage-node-{slug}", "role": "link",
            "name": "Onboarding stage",  # identical for all five (ambiguous)
            "kind": "link",
            "href": f"http://127.0.0.1:8971/customers/acme-robotics/"
                    f"onboarding/{slug}",
            "bbox": _stage_bbox(order),
            "fill": _STAGE_FILL[slug],
        })
    return els


class _Session:
    __slots__ = ("url", "history", "gone")

    def __init__(self):
        self.url = _HOME
        self.history = [_HOME]
        self.gone = False


class NorthstarProvider:
    """Deterministic in-process provider over the Northstar product store."""

    name = "northstar"
    _lock = threading.Lock()
    _sessions: dict[str, _Session] = {}
    _counter = 0

    # ── lifecycle ────────────────────────────────────────────────────────
    def create(self, *, ttl_seconds: int, profile: str = "") -> ProviderSession:
        with NorthstarProvider._lock:
            NorthstarProvider._counter += 1
            ref = f"northstar-{NorthstarProvider._counter}"
            NorthstarProvider._sessions[ref] = _Session()
        return ProviderSession(provider_ref=ref)

    def _get(self, ref: str) -> _Session:
        with self._lock:
            st = self._sessions.get(ref)
        if st is None or st.gone:
            raise ProviderError("session gone")
        return st

    def close(self, provider_ref: str) -> None:
        with self._lock:
            st = self._sessions.get(provider_ref)
            if st:
                st.gone = True

    # ── observation building ─────────────────────────────────────────────
    def _path(self, url: str) -> str:
        u = url.split("://", 1)[-1]
        return "/" + u.split("/", 1)[1] if "/" in u else "/"

    def _observe(self, st: _Session) -> RawObservation:
        from demos.northstar.product import store

        path = self._path(st.url).rstrip("/") or "/"
        title, dom, elements, primary = "Northstar", "", [], ""

        if path == "/":
            company = store.company()
            title = "Northstar — Customer Success"
            dom = (f"Northstar. Demo version {company.get('demo_version','')}. "
                   "Customer success workspace.")
            elements = [
                {"id": "nav-customers", "role": "link", "name": "Customers",
                 "kind": "link", "bbox": [40, 40, 180, 72],
                 "href": "http://127.0.0.1:8971/customers"},
                {"id": "nav-tasks", "role": "link", "name": "Tasks",
                 "kind": "link", "bbox": [200, 40, 320, 72],
                 "href": "http://127.0.0.1:8971/tasks"},
            ]
        elif path == "/customers":
            title = "Northstar — Customers"
            dom = "Customers. Acme Robotics."
            elements = [{"id": "customer-acme-robotics", "role": "link",
                         "name": "Acme Robotics", "kind": "link",
                         "bbox": [40, 100, 320, 140],
                         "href": "http://127.0.0.1:8971/customers/"
                                 "acme-robotics"}]
        elif path == "/customers/acme-robotics":
            cust = store.customer("acme-robotics") or {}
            title = "Acme Robotics"
            dom = (f"Acme Robotics. Health: {cust.get('health','at risk')}. "
                   "Blocker: WMS sandbox credentials for Data Integration.")
            elements = _stage_elements() + [
                {"id": "acme-blocker", "role": "note", "name": "Blocker banner",
                 "kind": "note", "bbox": [40, 200, 900, 240]},
            ]
            primary = f"stage-node-{_STAGE_TARGET}"  # the amber, 2nd node
        elif path.startswith("/customers/acme-robotics/onboarding/"):
            slug = path.rsplit("/", 1)[-1]
            stage = store.stage_by_slug(slug) or {}
            title = f"Acme — {stage.get('name', slug)}"
            state = stage.get("status", "")
            dom = f"Stage {stage.get('name', slug)}: {state}."
            if state == "blocked":
                elements = [{"id": "stage-blocker", "role": "note",
                             "name": "Blocked: WMS sandbox credentials",
                             "kind": "note", "bbox": [40, 120, 900, 160]}]
        elif path == "/tasks":
            title = "Northstar — Tasks"
            rows = store.tasks()
            dom = "Tasks. " + "; ".join(t["title"] for t in rows)
            elements = [
                {"id": f"task-{t['id']}", "role": "row", "name": t["title"],
                 "kind": "row", "bbox": [40, 100 + i * 44, 1100, 136 + i * 44]}
                for i, t in enumerate(rows)
            ]
            # The guarded follow-up control (a submit → policy classifies it
            # guarded → routed to canonical approval, never executed inline).
            elements.append({
                "id": "create-followup", "role": "button",
                "name": "Create follow-up task for Acme Robotics",
                "kind": "submit", "bbox": [40, 500, 420, 540]})
            # A confirmation banner appears once the task exists (post-write).
            from demos.northstar.product import seed

            if store.task_by_idempotency_key(seed.FOLLOWUP_IDEMPOTENCY_KEY):
                elements.append({"id": "task-confirmation", "role": "note",
                                 "name": "Follow-up task created",
                                 "kind": "note", "bbox": [40, 60, 900, 96]})
        elif path == "/security":
            title = "Northstar — Security"
            dom = "Security settings. External data sharing: off."
        else:
            title = "Not found"
            dom = "This page does not exist."

        perception = {
            "url": st.url, "viewport": _VIEWPORT,
            "primary_visual_target": primary,
            "elements": [{"id": e["id"], "name": e.get("name", ""),
                          "bbox": e.get("bbox"), "fill": e.get("fill")}
                         for e in elements],
        }
        a11y = " | ".join(f"{e.get('role','')}: {e.get('name','')}".strip()
                          for e in elements)[:1000]
        return RawObservation(
            url=st.url, title=title, dom_summary=dom[:2000],
            elements=elements, screenshot_ref=f"northstar-shot:{path}",
            truncated=False, viewport=dict(_VIEWPORT),
            accessibility_summary=f"{title} | {a11y}"[:1000],
            screenshot_bytes=json.dumps(perception,
                                        separators=(",", ":")).encode())

    # ── provider API ─────────────────────────────────────────────────────
    def observe(self, provider_ref: str) -> RawObservation:
        return self._observe(self._get(provider_ref))

    def navigate(self, provider_ref: str, url: str) -> RawObservation:
        st = self._get(provider_ref)
        st.url = url
        st.history.append(url)
        return self._observe(st)

    def click(self, provider_ref: str, element_id: str) -> RawObservation:
        st = self._get(provider_ref)
        obs = self._observe(st)
        for el in obs.elements:
            if el["id"] == element_id and el.get("href"):
                st.url = el["href"]
                st.history.append(el["href"])
                break
        return self._observe(st)

    def type_text(self, provider_ref: str, element_id: str,
                  text: str) -> RawObservation:
        return self._observe(self._get(provider_ref))

    def scroll(self, provider_ref: str, direction: str,
               amount: int = 1) -> RawObservation:
        return self._observe(self._get(provider_ref))

    def go_back(self, provider_ref: str) -> RawObservation:
        st = self._get(provider_ref)
        if len(st.history) > 1:
            st.history.pop()
            st.url = st.history[-1]
        return self._observe(st)

    def wait(self, provider_ref: str, seconds: float = 0.0) -> RawObservation:
        return self._observe(self._get(provider_ref))

    def inspect(self, provider_ref: str, target: str = "") -> RawObservation:
        return self._observe(self._get(provider_ref))

    def viewer(self, provider_ref: str) -> dict:
        st = self._get(provider_ref)
        obs = self._observe(st)
        # Read-only viewer payload: rendered text only, no provider internals.
        return {"kind": "northstar_readonly", "url": st.url,
                "title": obs.title, "rendered_text": obs.dom_summary[:2000],
                "read_only": True}

    @classmethod
    def _reset(cls) -> None:
        with cls._lock:
            cls._sessions.clear()
            cls._counter = 0
