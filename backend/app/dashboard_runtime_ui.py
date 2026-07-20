"""Small runtime enhancements for the owner dashboard HTML.

The dashboard is intentionally a single checked-in HTML file.  This module keeps
one narrowly-scoped product preference out of that large file: the Action Center
shows actions from the eight most recent meetings by default, while retaining a
button to browse the complete history.

The middleware changes presentation only.  It never changes, truncates, or
removes canonical action or meeting records from the backend response.
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request
from starlette.responses import Response

_CLOSE_MARKER = "\n})();\n</script>"

_ACTION_CENTER_SCRIPT = r"""
  // Product default: keep the Action Center focused on the eight most recent
  // meetings, while preserving one-click access to the full action history.
  var _acOriginalActionIndex = actionIndex;
  var _acBrowseOlderMeetings = false;
  function _acRecentMeetingActions(){
    var meetings=((DATA&&DATA.meetings)||[]).slice().sort(function(a,b){
      return Number(b.saved_at||0)-Number(a.saved_at||0);
    }).slice(0,8);
    var out=[];
    meetings.forEach(function(m){
      (m.actions||[]).forEach(function(a){ out.push({a:a,m:m}); });
    });
    return out;
  }
  var _acOriginalRenderActions = renderActions;
  renderActions = function(){
    var savedActionIndex=actionIndex;
    actionIndex=_acBrowseOlderMeetings?_acOriginalActionIndex:_acRecentMeetingActions;
    try { _acOriginalRenderActions(); }
    finally { actionIndex=savedActionIndex; }

    var box=$("#ac-list");
    var allMeetings=((DATA&&DATA.meetings)||[]).slice().sort(function(a,b){
      return Number(b.saved_at||0)-Number(a.saved_at||0);
    });
    if(!box||allMeetings.length<=8) return;
    var footer=document.createElement("div");
    footer.className="ac-history-toggle";
    footer.style.cssText="display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 4px 2px;color:var(--faint);font-size:.8rem;flex-wrap:wrap";
    var label=document.createElement("span");
    label.textContent=_acBrowseOlderMeetings
      ? "Showing actions from all "+allMeetings.length+" meetings."
      : "Showing actions from the latest 8 meetings.";
    var button=document.createElement("button");
    button.type="button";
    button.className="btn";
    button.textContent=_acBrowseOlderMeetings?"Show latest 8":"Browse older meetings";
    button.addEventListener("click",function(){
      _acBrowseOlderMeetings=!_acBrowseOlderMeetings;
      renderActions();
    });
    footer.appendChild(label);
    footer.appendChild(button);
    box.appendChild(footer);
  };
"""


def enhance_dashboard_html(html: str) -> str:
    """Inject the recent-meeting Action Center preference into dashboard HTML."""
    if _CLOSE_MARKER not in html or "function renderActions()" not in html:
        return html
    before, marker, after = html.rpartition(_CLOSE_MARKER)
    return before + "\n" + _ACTION_CENTER_SCRIPT + marker + after


def install(app: FastAPI) -> None:
    """Install the Action Center enhancer and Laura-owned approval guard."""
    # main.py imports the routers before security.install(app), so this can patch
    # the two legacy approval seams deterministically during app construction.
    from . import approval_runtime_guard

    approval_runtime_guard.install(app)

    @app.middleware("http")
    async def _dashboard_recent_meetings(
        request: Request, call_next: Callable
    ) -> Response:
        response = await call_next(request)
        if request.method != "GET" or request.url.path.rstrip("/") != "/dashboard":
            return response
        content_type = str(response.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            return response

        body = getattr(response, "body", None)
        if body is None:
            chunks = [chunk async for chunk in response.body_iterator]
            body = b"".join(chunks)
        try:
            html = body.decode("utf-8")
        except UnicodeDecodeError:
            return response
        enhanced = enhance_dashboard_html(html)
        if enhanced == html:
            return response

        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=enhanced,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
            background=response.background,
        )
