"""Server-side HTML for the Northstar demo product.

Design rules that the tests and the visual-eyes challenge depend on:
  * every interactive element carries a STABLE id and data-testid;
  * URLs are predictable (see app.py routes);
  * the onboarding *diagram* is the visual-only fixture — its nodes share one
    identical accessible label and carry NO stage-name text, so the stage a
    reader must click is knowable only from fill colour + position. The fully
    accessible counterpart is the onboarding *checklist* page.

No external requests: all CSS is inline, there are no images, no fonts, no JS
beyond a tiny inline handler for the guarded-task buttons.
"""
from __future__ import annotations

import html
import json
from typing import Any

from . import seed, store

_CSS = """
:root{--bg:#f5f6f8;--card:#fff;--ink:#1a1c22;--soft:#5c6270;--line:#dfe1e8;
--accent:#2f5bd0;--good:#2f9e5b;--warn:#c8811f;--pend:#8a90a2;--bad:#c0392b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 ui-sans-serif,system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header.top{background:#12151c;color:#fff;padding:12px 20px;display:flex;
align-items:center;gap:18px;flex-wrap:wrap}
header.top .brand{font-weight:700;letter-spacing:-.01em}
nav a{color:#c9cede;font-size:14px;padding:4px 2px}
nav a:hover,nav a[aria-current=page]{color:#fff;text-decoration:none;
border-bottom:2px solid #4f80ff}
main{max-width:920px;margin:0 auto;padding:26px 20px 64px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:16px;margin:26px 0 10px}
.sub{color:var(--soft);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin:0 0 14px}
.row{display:flex;justify-content:space-between;gap:12px;align-items:center;
padding:8px 0;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.pill{font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px;
border:1px solid var(--line);white-space:nowrap}
.st-done{color:var(--good);border-color:var(--good)}
.st-blocked{color:var(--warn);border-color:var(--warn)}
.st-pending,.st-not_started{color:var(--pend);border-color:var(--pend)}
.st-open{color:var(--accent);border-color:var(--accent)}
.st-at_risk{color:var(--warn);border-color:var(--warn)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.kv{font-size:13px}.kv dt{color:var(--soft);text-transform:uppercase;
font-size:11px;letter-spacing:.05em}.kv dd{margin:2px 0 10px;font-weight:600}
.btn{font:inherit;font-weight:600;border:1px solid var(--accent);
background:var(--accent);color:#fff;border-radius:8px;padding:9px 14px;cursor:pointer}
.btn.secondary{background:#fff;color:var(--accent)}
.banner{border-radius:8px;padding:12px 14px;margin:0 0 14px;font-size:14px}
.banner.ok{background:#e8f6ee;border:1px solid var(--good);color:#1f6b3d}
.banner.info{background:#eef2fc;border:1px solid var(--accent);color:#274690}
.muted{color:var(--soft);font-size:13px}
svg .stage-node{cursor:pointer}
svg .stage-node:focus{outline:3px solid #4f80ff;outline-offset:2px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 6px;
border-top:1px solid var(--line);font-size:14px}th{color:var(--soft);
font-size:11px;text-transform:uppercase;letter-spacing:.05em}
"""

_NAV = [
    ("nav-home", "/", "Home"),
    ("nav-customers", "/customers", "Customers"),
    ("nav-acme", "/customers/acme-robotics", "Acme Robotics"),
    ("nav-onboarding", "/customers/acme-robotics/onboarding", "Onboarding"),
    ("nav-implementation", "/customers/acme-robotics/implementation", "Implementation"),
    ("nav-tasks", "/tasks", "Tasks"),
    ("nav-security", "/security", "Security"),
]


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _pill(status: str, text: str | None = None) -> str:
    return (f'<span class="pill st-{_esc(status)}" data-status="{_esc(status)}">'
            f'{_esc(text or status.replace("_", " "))}</span>')


def page(title: str, body: str, current: str = "") -> str:
    nav = "".join(
        f'<a data-testid="{tid}" href="{href}"'
        f'{" aria-current=page" if tid == current else ""}>{_esc(label)}</a>'
        for tid, href, label in _NAV
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)} · Northstar</title><style>{_CSS}</style></head><body>"
        f'<header class=top><span class=brand data-testid="brand">Northstar</span>'
        f"<nav aria-label=Primary>{nav}</nav></header>"
        f'<main data-testid="page-{_esc(current.replace("nav-", "") or "root")}">{body}</main>'
        "</body></html>"
    )


# ── pages ──────────────────────────────────────────────────────────────────
def home() -> str:
    c = store.company()
    body = (
        f'<h1 data-testid="page-title">{_esc(c["name"])}</h1>'
        f'<p class=sub>{_esc(c["tagline"])}</p>'
        '<div class=card><div class=grid>'
        f'<div class=kv><dt>Product</dt><dd>{_esc(c["product"])}</dd></div>'
        f'<div class=kv><dt>Customers</dt><dd data-testid="stat-customers">'
        f'{len(store.customers())}</dd></div>'
        f'<div class=kv><dt>Demo version</dt><dd data-testid="demo-version">'
        f'{seed.DEMO_VERSION}</dd></div></div></div>'
        '<p class=muted>Synthetic environment for Laura’s end-to-end demo. '
        'All data is fictional and resettable.</p>'
    )
    return page("Home", body, "nav-home")


def customers() -> str:
    rows = "".join(
        f'<div class=row data-testid="customer-{_esc(c["id"])}">'
        f'<a href="/customers/{_esc(c["id"])}">{_esc(c["name"])}</a>'
        f'<span>{_esc(c["industry"])}</span>{_pill(c["health"])}</div>'
        for c in store.customers()
    )
    return page("Customers",
                f'<h1 data-testid="page-title">Customers</h1>'
                f'<div class=card>{rows}</div>', "nav-customers")


def _onboarding_diagram() -> str:
    """The VISUAL-ONLY fixture. Five nodes, one shared accessible label, no
    stage-name text — the blocked stage is identifiable only by fill+position.
    Each node links to its own stage page, so the accessible layer cannot tell
    you which link is Data Integration."""
    W, H, r, gap, x0 = 720, 120, 26, 150, 90
    nodes, edges = [], []
    stages = store.onboarding_stages()
    for i, s in enumerate(stages):
        cx = x0 + s["visual"]["order"] * gap
        cy = 60
        if i > 0:
            px = x0 + stages[i - 1]["visual"]["order"] * gap
            edges.append(f'<line x1="{px + r}" y1="{cy}" x2="{cx - r}" y2="{cy}" '
                         f'stroke="#c4c8d4" stroke-width="3"/>')
        # role=link + IDENTICAL aria-label; no <title>/<text> stage name inside.
        nodes.append(
            f'<a class="stage-node" data-testid="stage-node" '
            f'id="{_esc(s["id"])}" href="/customers/acme-robotics/onboarding/'
            f'{_esc(s["slug"])}" role="link" aria-label="Onboarding stage" '
            f'tabindex="0" data-status="{_esc(s["status"])}" '
            f'data-fill="{_esc(s["visual"]["fill"])}" '
            f'data-order="{s["visual"]["order"]}">'
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{_esc(s["visual"]["fill"])}" '
            f'stroke="#12151c" stroke-width="1.5"/></a>'
        )
    return (
        '<figure class=card style="margin:0 0 14px">'
        '<figcaption class=muted>Onboarding pipeline (visual). The blocked stage '
        'is shown in amber — read the diagram to find it.</figcaption>'
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="group" '
        f'aria-label="Acme Robotics onboarding pipeline" '
        f'data-testid="onboarding-diagram">'
        f'{"".join(edges)}{"".join(nodes)}</svg></figure>'
    )


def acme_account() -> str:
    c = store.customer(seed.CUSTOMER_ID)
    if c is None:
        return page("Not found", "<h1>Customer not found</h1>", "nav-customers")
    kv = "".join(
        f'<div class=kv><dt>{_esc(k)}</dt><dd data-testid="acme-{_esc(t)}">'
        f'{_esc(v)}</dd></div>'
        for k, t, v in [
            ("Plan", "plan", c["plan"]),
            ("Health", "health", c["health"].replace("_", " ")),
            ("CSM", "csm", c["csm"]),
            ("Implementation", "impl-eng", c["implementation_engineer"]),
            ("Sponsor", "sponsor", c["sponsor"]),
            ("Target go-live", "go-live", c["target_go_live"]),
        ]
    )
    body = (
        f'<h1 data-testid="page-title">{_esc(c["name"])}</h1>'
        f'<p class=sub>{_esc(c["industry"])} · {_pill(c["health"])}</p>'
        f'<div class=card><div class=grid>{kv}</div></div>'
        f'<div class="banner info" data-testid="acme-blocker">'
        f'<strong>Current blocker:</strong> {_esc(c["blocker"])}</div>'
        '<h2>Onboarding pipeline</h2>'
        f'{_onboarding_diagram()}'
        '<p class=muted>For the accessible, labelled view see the '
        '<a href="/customers/acme-robotics/onboarding">Onboarding checklist</a>.</p>'
    )
    return page("Acme Robotics", body, "nav-acme")


def onboarding_checklist() -> str:
    rows = "".join(
        f'<div class=row data-testid="{_esc(i["id"])}">'
        f'<span>{_esc(i["label"])}</span>{_pill(i["status"])}</div>'
        for i in store.onboarding_checklist()
    )
    body = (
        '<h1 data-testid="page-title">Acme Robotics — Onboarding checklist</h1>'
        '<p class=sub>Accessible, fully labelled counterpart to the account '
        'pipeline diagram.</p>'
        f'<div class=card>{rows}</div>'
    )
    return page("Onboarding checklist", body, "nav-onboarding")


def onboarding_stage(slug: str) -> str:
    s = store.stage_by_slug(slug)
    if s is None:
        return page("Stage not found",
                    '<h1 data-testid="page-title">Stage not found</h1>'
                    '<p><a href="/customers/acme-robotics">Back to Acme</a></p>',
                    "nav-acme")
    extra = ""
    if s["slug"] == "integration":
        c = store.customer(seed.CUSTOMER_ID)
        extra = (f'<div class="banner info" data-testid="stage-blocker">'
                 f'<strong>Blocker:</strong> {_esc(c["blocker"])}</div>')
    body = (
        f'<h1 data-testid="page-title">Onboarding stage: {_esc(s["name"])}</h1>'
        f'<p class=sub data-testid="stage-name">{_esc(s["name"])} '
        f'{_pill(s["status"])}</p>{extra}'
        f'<div class=card data-testid="stage-{_esc(s["slug"])}">'
        f'<div class=row><span>Stage id</span><code>{_esc(s["id"])}</code></div>'
        f'<div class=row><span>Status</span>{_pill(s["status"])}</div></div>'
        '<p><a href="/customers/acme-robotics">Back to Acme Robotics</a></p>'
    )
    return page(f"Stage · {s['name']}", body, "nav-acme")


def implementation() -> str:
    im = store.implementation()
    r = im["go_live_readiness"]
    rows = "".join(
        f'<div class=row data-testid="{_esc(m["id"])}">'
        f'<span><strong>{_esc(m["name"])}</strong><br>'
        f'<span class=muted>{_esc(m["detail"])}</span></span>'
        f'{_pill(m["status"])}</div>'
        for m in im["milestones"]
    )
    body = (
        '<h1 data-testid="page-title">Acme Robotics — Implementation status</h1>'
        f'<p class=sub>Go-live readiness: '
        f'<strong data-testid="go-live-readiness">{r["complete"]}/{r["total"]}'
        f'</strong></p><div class=card>{rows}</div>'
    )
    return page("Implementation status", body, "nav-implementation")


def tasks(flash: dict[str, Any] | None = None) -> str:
    banner = ""
    if flash and flash.get("task"):
        t = flash["task"]
        verb = "Created" if flash.get("created") else "Already present"
        banner = (f'<div class="banner ok" data-testid="task-confirmation">'
                  f'{verb}: <strong>{_esc(t["title"])}</strong> '
                  f'({_esc(t["id"])})</div>')
    rows = "".join(
        f'<tr data-testid="task-{_esc(t["id"])}" '
        f'data-idem="{_esc(t.get("idempotency_key",""))}">'
        f'<td>{_esc(t["id"])}</td><td>{_esc(t["title"])}</td>'
        f'<td>{_esc(t["assignee"])}</td><td>{_pill(t["status"])}</td>'
        f'<td class=muted>{_esc(t["created_at"])}</td></tr>'
        for t in store.tasks()
    )
    preview = json.dumps({
        "title": seed.FOLLOWUP_TITLE, "note": seed.FOLLOWUP_NOTE,
        "assignee": seed.FOLLOWUP_ASSIGNEE,
        "idempotency_key": seed.FOLLOWUP_IDEMPOTENCY_KEY,
    })
    # Tiny inline handler — the ONLY script, no external calls, same-origin fetch.
    script = (
        "<script>"
        "async function nsPreview(){"
        "let r=await fetch('/api/acme/tasks/preview',{method:'POST',"
        "headers:{'content-type':'application/json'},body:'{}'});"
        "let j=await r.json();"
        "document.getElementById('ns-preview').textContent=JSON.stringify(j,null,2);}"
        "async function nsCreate(){"
        "let r=await fetch('/api/acme/tasks',{method:'POST',"
        "headers:{'content-type':'application/json'},"
        f"body:JSON.stringify({{idempotency_key:{json.dumps(seed.FOLLOWUP_IDEMPOTENCY_KEY)}}})}});"
        "if(r.ok){location.href='/tasks?created=1';}}"
        "</script>"
    )
    body = (
        '<h1 data-testid="page-title">Tasks</h1>'
        f'{banner}'
        f'<table data-testid="tasks-table"><thead><tr><th>Id</th><th>Title</th>'
        f'<th>Assignee</th><th>Status</th><th>Created</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        '<h2>Guarded operation — create Acme follow-up task</h2>'
        '<p class=muted>Preview is read-only. Creation is idempotent on a stable '
        'key: repeated approved execution yields exactly one task.</p>'
        '<div class=card>'
        '<button class="btn secondary" data-testid="btn-preview" '
        'onclick="nsPreview()">Preview task</button> '
        '<button class="btn" data-testid="btn-create" onclick="nsCreate()">'
        'Approve &amp; create</button>'
        f'<pre id="ns-preview" data-testid="preview-output" '
        f'class=muted style="white-space:pre-wrap;margin-top:12px"></pre></div>'
        f'{script}'
    )
    return page("Tasks", body, "nav-tasks")


def security() -> str:
    rows = "".join(
        f'<div class=row data-testid="{_esc(s["id"])}">'
        f'<span>{_esc(s["label"])}</span>'
        f'<span class="pill st-{"done" if s["state"]=="on" else "pending"}" '
        f'role=status aria-label="{_esc(s["label"])}: {_esc(s["state"])}">'
        f'{_esc(s["state"].upper())}</span></div>'
        for s in store.security_settings()
    )
    body = (
        '<h1 data-testid="page-title">Security settings</h1>'
        '<p class=sub>Read-only posture for the demo environment.</p>'
        f'<div class=card>{rows}</div>'
    )
    return page("Security settings", body, "nav-security")
