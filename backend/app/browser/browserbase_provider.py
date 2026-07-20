"""Real remote-browser adapter behind BrowserProvider (B1) — Browserbase+CDP.

Implements the ACTUAL remote-browser connection, real screenshot acquisition,
bounded server-side image handling, navigation and observation — behind the
same BrowserProvider interface B0 defined. It is DELIBERATELY INERT WITHOUT
KEYS: every method raises ``ProviderUnconfigured`` until
``BROWSER_REAL_PROVIDER_ENABLED`` is on AND
``BROWSERBASE_API_KEY``/``BROWSERBASE_PROJECT_ID`` are configured. The whole
CI suite therefore runs on the fake provider and needs no browser credentials.

Real-pixel behaviour is UNPROVEN until the credential-gated smoke runs (see
docs/product/BROWSER-B1-VISUAL-EYES.md). Do NOT substitute fake-provider
results for the real-pixels acceptance gate.

Security (design review, enforced here):
- observe() sources ONLY visible text + the accessibility tree + declared
  element geometry. It NEVER reads the cookie jar, request/response headers,
  localStorage, sessionStorage, IndexedDB, or network traffic into the
  observation. Credentials therefore never enter the observation at the source
  (policy.redact is only a backstop).
- The API key + the CDP connect URL are secrets: they live only in memory for
  the call, are never logged, never returned, never persisted, never placed in
  an error string or receipt. provider_ref is the Browserbase session id
  (opaque), never a bearer URL.
- Screenshots are captured at a bounded viewport; the raw bytes are handed to
  the operator transiently and are bounded/downscaled by the multimodal
  adapter before any model transmission. The viewer live-view URL is minted
  short-lived and returned once through the token exchange, never stored.
"""
from __future__ import annotations

from typing import Any

from ..config import settings
from .provider import (
    BrowserProvider,
    ProviderError,
    ProviderSession,
    ProviderUnconfigured,
    RawObservation,
)

_CDP_TIMEOUT_MS = 30_000
_CURSOR_GLIDE_MS = 650  # let the pointer visibly travel before the click lands

# A cosmetic pointer overlay so a viewer watching the live view can FOLLOW the
# avatar: a red arrow that glides (CSS transition) to the target element's
# centre, with a brief pulse ring. Injected into the page transiently; it reads
# nothing and touches no cookie/storage/network channel. Re-created on demand
# (a navigation drops it). Returns true when the target was found + moved to.
_CURSOR_JS = r"""
(sel) => {
  let c = document.getElementById('__laura_cursor');
  if (!c) {
    c = document.createElement('div');
    c.id = '__laura_cursor';
    c.style.cssText = 'position:fixed;z-index:2147483647;width:24px;height:24px;'
      + 'margin:-3px 0 0 -3px;pointer-events:none;left:50%;top:50%;'
      + 'transition:left .55s cubic-bezier(.4,0,.2,1),top .55s cubic-bezier(.4,0,.2,1);'
      + 'filter:drop-shadow(0 1px 2px rgba(0,0,0,.5));background:no-repeat center/contain;'
      + "background-image:url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' width='24' height='24'><path d='M4 2l16 8-7 2-2 7z' fill='%23ff3b30' stroke='white' stroke-width='1.3'/></svg>\")";
    document.body.appendChild(c);
    const s = document.createElement('style');
    s.textContent = '@keyframes __laura_pulse{0%{transform:scale(.4);opacity:.8}100%{transform:scale(1.8);opacity:0}}';
    document.head.appendChild(s);
  }
  const el = document.querySelector(sel);
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  c.style.left = x + 'px';
  c.style.top = y + 'px';
  const ring = document.createElement('div');
  ring.style.cssText = 'position:fixed;z-index:2147483646;width:28px;height:28px;'
    + 'margin:-14px 0 0 -14px;border:3px solid #ff3b30;border-radius:50%;'
    + 'pointer-events:none;left:' + x + 'px;top:' + y + 'px;'
    + 'animation:__laura_pulse .6s ease-out .5s forwards';
  document.body.appendChild(ring);
  setTimeout(() => ring.remove(), 1300);
  return true;
}
"""

# Playwright JS run in the page to extract a SAFE observation. It reads only
# the accessibility-relevant DOM (visible text + interactive elements + their
# viewport bounding boxes) and DELIBERATELY touches no cookie/storage/header
# channel. Kept as a string so the module imports without Playwright present.
#
# Two adversarial-hardening rules baked in here:
# 1. Each enumerated node is stamped with a SERVER-MINTED synthetic id
#    (`data-laura-el="el-N"`) and that synthetic id is returned as `id` — the
#    page's own id/class is NEVER used to address the element, so a page cannot
#    smuggle CSS metacharacters that would make the executor act on a different
#    (guarded) node than the one policy classified.
# 2. The element name is NEVER sourced from `el.value` — a credential/OTP field
#    value (plaintext behind the mask) must never enter the observation.
_OBSERVE_JS = r"""
() => {
  const vp = { width: window.innerWidth, height: window.innerHeight };
  const roleOf = (el) => el.getAttribute('role') ||
    ({A:'link',BUTTON:'button',INPUT:'textbox',SELECT:'combobox',
      TEXTAREA:'textbox'}[el.tagName] || el.tagName.toLowerCase());
  const kindOf = (el) => {
    const t = (el.getAttribute('type')||'').toLowerCase();
    const ac = (el.getAttribute('autocomplete')||'').toLowerCase();
    const name = (el.getAttribute('name')||'').toLowerCase();
    if (t==='password' || ac.includes('current-password') ||
        ac.includes('new-password') || /pass|secret|token/.test(name))
      return 'credential';
    if (ac.includes('one-time-code') || /otp|mfa|2fa/.test(name)) return 'mfa';
    if (el.tagName==='BUTTON' && el.type==='submit') return 'submit';
    if (el.tagName==='A') return 'link';
    if (el.tagName==='INPUT' || el.tagName==='TEXTAREA') return 'text_input';
    return el.tagName.toLowerCase();
  };
  const nodes = [...document.querySelectorAll(
    'a,button,input,select,textarea,[role=button],[role=link]')].slice(0,60);
  const elements = nodes.map((el, i) => {
    const r = el.getBoundingClientRect();
    const synth = 'el-' + i;
    el.setAttribute('data-laura-el', synth);  // server-minted addressing handle
    // NEVER read el.value — a masked password/OTP value is still plaintext here.
    const name = (el.getAttribute('aria-label') || el.innerText ||
                  el.getAttribute('placeholder') || '').trim().slice(0,120);
    return {
      id: synth,
      role: roleOf(el),
      name: name,
      kind: kindOf(el),
      href: el.tagName==='A' ? el.href : undefined,
      bbox: [Math.round(r.left), Math.round(r.top),
             Math.round(r.right), Math.round(r.bottom)]
    };
  }).filter(e => e.bbox[2] > e.bbox[0]);
  return {
    url: location.href,
    title: document.title,
    viewport: vp,
    dom_summary: (document.body ? document.body.innerText : '').slice(0, 4000),
    a11y: elements.map(e => e.role + ': ' + e.name).join(' | ').slice(0,1000),
    elements
  };
}
"""


def _el_selector(element_id: str):
    """Address an element by its SERVER-MINTED synthetic handle — never by a
    page-controlled id interpolated into a CSS selector (which could expand to
    a selector-list and act on a different, guarded node). Playwright treats
    the quoted attribute value as a literal exact match."""
    import json as _json

    return f"[data-laura-el={_json.dumps(str(element_id))}]"


def _require_config() -> None:
    if not settings.browser_real_provider_enabled:
        raise ProviderUnconfigured(
            "browser_real_provider_enabled is off (B0/CI use the fake provider)"
        )
    if not settings.browserbase_api_key or not settings.browserbase_project_id:
        raise ProviderUnconfigured(
            "BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID are not configured"
        )


class BrowserbaseProvider(BrowserProvider):  # type: ignore[misc]
    name = "browserbase"

    # In-process registry of live Playwright connections, keyed by the opaque
    # Browserbase session id (provider_ref). Never keyed by URL/org.
    _live: dict[str, Any] = {}

    # ── session lifecycle ────────────────────────────────────────────────

    def create(self, *, ttl_seconds: int, profile: str = "") -> ProviderSession:
        _require_config()
        session_id, connect_url = self._create_remote_session(
            ttl_seconds, profile)
        page = self._connect(connect_url)  # connect_url used here, never stored
        BrowserbaseProvider._live[session_id] = page
        return ProviderSession(provider_ref=session_id)

    def _create_remote_session(self, ttl_seconds: int,
                               profile: str = "") -> tuple[str, str]:
        try:
            import httpx
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnconfigured("http client unavailable") from exc
        browser_settings: dict = {"viewport": {"width": 1280, "height": 720}}
        if profile:
            # A provider-side persistent Context (saved browser login). The
            # context id is a secret-adjacent handle: used here, never logged.
            browser_settings["context"] = {"id": profile, "persist": True}
        try:
            resp = httpx.post(
                "https://api.browserbase.com/v1/sessions",
                headers={"X-BB-API-Key": settings.browserbase_api_key,
                         "Content-Type": "application/json"},
                json={"projectId": settings.browserbase_project_id,
                      "browserSettings": browser_settings},
                timeout=30)
        except Exception as exc:  # noqa: BLE001 — never surface the payload
            raise ProviderError("browser session create failed") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"browser session http {resp.status_code}")
        data = resp.json()
        session_id = str(data.get("id") or "")
        connect_url = str(data.get("connectUrl") or "")
        if not session_id or not connect_url:
            raise ProviderError("browser session missing id/connectUrl")
        return session_id, connect_url

    def _connect(self, connect_url: str):
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnconfigured("playwright not installed") from exc
        # The connect_url is a bearer capability — used here, never stored/logged.
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(connect_url,
                                               timeout=_CDP_TIMEOUT_MS)
        context = browser.contexts[0] if browser.contexts \
            else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        page._laura_pw = pw  # keep the driver alive with the page
        return page

    def _page(self, provider_ref: str):
        page = BrowserbaseProvider._live.get(provider_ref)
        if page is None:
            raise ProviderError("session gone")
        return page

    # ── reads / actions ──────────────────────────────────────────────────

    def _observe_page(self, page) -> RawObservation:
        try:
            data = page.evaluate(_OBSERVE_JS)
            shot = page.screenshot(type="png")  # bounded viewport
        except Exception as exc:  # noqa: BLE001 — never surface page internals
            raise ProviderError("observe failed") from exc
        return RawObservation(
            url=str(data.get("url") or ""),
            title=str(data.get("title") or ""),
            dom_summary=str(data.get("dom_summary") or "")[:4000],
            elements=list(data.get("elements") or []),
            screenshot_ref=f"bb:{id(page)}",  # opaque, byte-free handle
            truncated=len(data.get("elements") or []) >= 60,
            viewport=dict(data.get("viewport") or {"width": 1280,
                                                   "height": 720}),
            accessibility_summary=str(data.get("a11y") or "")[:1000],
            screenshot_bytes=shot or b"",
        )

    def observe(self, provider_ref: str) -> RawObservation:
        _require_config()
        return self._observe_page(self._page(provider_ref))

    def navigate(self, provider_ref: str, url: str) -> RawObservation:
        _require_config()
        page = self._page(provider_ref)
        try:
            page.goto(url, timeout=_CDP_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("navigate failed") from exc
        return self._observe_page(page)

    def click(self, provider_ref: str, element_id: str) -> RawObservation:
        _require_config()
        page = self._page(provider_ref)
        selector = _el_selector(element_id)
        try:
            self._show_cursor(page, selector)  # visible pointer glides in first
            page.click(selector, timeout=_CDP_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("click failed") from exc
        return self._observe_page(page)

    def _show_cursor(self, page, selector: str) -> None:
        """Render a visible pointer that glides to the target so a viewer
        watching the live view can FOLLOW what the avatar is doing. Best-effort
        cosmetic overlay — a failure never blocks the click."""
        try:
            moved = page.evaluate(_CURSOR_JS, selector)
            if moved:
                page.wait_for_timeout(_CURSOR_GLIDE_MS)  # let the glide land
        except Exception:  # noqa: BLE001 — cosmetic only
            pass

    def type_text(self, provider_ref: str, element_id: str,
                  text: str) -> RawObservation:
        _require_config()
        page = self._page(provider_ref)
        try:
            page.fill(_el_selector(element_id), text, timeout=_CDP_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("type failed") from exc
        return self._observe_page(page)

    def scroll(self, provider_ref: str, direction: str,
               amount: int = 1) -> RawObservation:
        _require_config()
        page = self._page(provider_ref)
        dy = 600 * (amount if direction == "down" else -amount)
        try:
            page.mouse.wheel(0, dy)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("scroll failed") from exc
        return self._observe_page(page)

    def go_back(self, provider_ref: str) -> RawObservation:
        _require_config()
        page = self._page(provider_ref)
        try:
            page.go_back(timeout=_CDP_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — back at history root is not fatal
            pass
        return self._observe_page(page)

    def wait(self, provider_ref: str, seconds: float = 0.0) -> RawObservation:
        _require_config()
        page = self._page(provider_ref)
        try:
            page.wait_for_timeout(min(float(seconds), 5.0) * 1000)
        except Exception:  # noqa: BLE001
            pass
        return self._observe_page(page)

    def inspect(self, provider_ref: str, target: str = "") -> RawObservation:
        _require_config()
        return self._observe_page(self._page(provider_ref))

    def create_context(self) -> str:
        """Mint a provider-side persistent Context (saved browser profile).
        Returns the context id — stored server-side as an identity's
        context_ref, never returned by any API or logged."""
        _require_config()
        try:
            import httpx
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnconfigured("http client unavailable") from exc
        try:
            resp = httpx.post(
                "https://api.browserbase.com/v1/contexts",
                headers={"X-BB-API-Key": settings.browserbase_api_key,
                         "Content-Type": "application/json"},
                json={"projectId": settings.browserbase_project_id},
                timeout=30)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("context create failed") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"context create http {resp.status_code}")
        ctx = str((resp.json() or {}).get("id") or "")
        if not ctx:
            raise ProviderError("context create missing id")
        return ctx

    def viewer(self, provider_ref: str) -> dict[str, Any]:
        """The Browserbase live-view URL for this session, minted
        server-side and returned ONCE through the presentation-token
        exchange (_safe_viewer allowlists only kind/url/title). The debug
        URL dies with the session; it is never logged or stored."""
        _require_config()
        try:
            import httpx
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnconfigured("http client unavailable") from exc
        try:
            resp = httpx.get(
                f"https://api.browserbase.com/v1/sessions/{provider_ref}/debug",
                headers={"X-BB-API-Key": settings.browserbase_api_key},
                timeout=15)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("viewer mint failed") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"viewer mint http {resp.status_code}")
        data = resp.json() or {}
        url = str(data.get("debuggerFullscreenUrl")
                  or data.get("debuggerUrl") or "")
        if not url:
            raise ProviderError("viewer mint missing url")
        return {"kind": "live", "url": url}

    def close(self, provider_ref: str) -> None:
        _require_config()
        page = BrowserbaseProvider._live.pop(provider_ref, None)
        if page is not None:
            try:
                pw = getattr(page, "_laura_pw", None)
                page.context.browser.close()
                if pw:
                    pw.stop()
            except Exception:  # noqa: BLE001 — best-effort release
                pass
