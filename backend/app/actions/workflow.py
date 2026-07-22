"""Post-meeting workflow actions: actually send the follow-up email + Slack.

Both are simple HTTP calls and are no-ops (with a clear reason) until you set the
matching key/URL in .env, so the app never crashes for not having them:

  SENDGRID_API_KEY + MAIL_FROM   -> send the follow-up email
  SLACK_WEBHOOK_URL              -> post the checklist to a Slack channel

Notion/Jira are intentionally left as the same shape to add later.
"""
from __future__ import annotations

import httpx

from ..config import settings


def send_email(to: list[str], subject: str, body: str) -> dict:
    """Send an email via SendGrid. Returns {sent, ...}. No key → not sent."""
    if not settings.sendgrid_api_key or not settings.mail_from:
        return {"sent": False, "reason": "SENDGRID_API_KEY / MAIL_FROM not set"}
    if not to:
        return {"sent": False, "reason": "no recipients"}
    resp = httpx.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {settings.sendgrid_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": e} for e in to]}],
            "from": {"email": settings.mail_from},
            "subject": subject or "Meeting follow-up",
            "content": [{"type": "text/plain", "value": body or ""}],
        },
        timeout=30.0,
    )
    ok = 200 <= resp.status_code < 300
    return {"sent": ok, "status_code": resp.status_code,
            "error": None if ok else resp.text[:300]}


def post_to_slack(text: str, org_id: str = "") -> dict:
    """Post to Slack via the deployment's incoming webhook.

    That webhook points at ONE workspace — the deployment owner's. Serving it
    to an arbitrary tenant would publish that tenant's meeting content into
    someone else's Slack (security audit 2026-07-23, gap #2), so it is scoped
    to the deployment's own (demo/key-free) org. A real tenant posts to Slack
    through its own org-scoped Cedric connection instead.
    """
    org = (org_id or "").strip()
    if org and org != str(settings.demo_org_id or "").strip():
        return {"sent": False, "reason": "slack is org-scoped through Cedric"}
    if not settings.slack_webhook_url:
        return {"sent": False, "reason": "SLACK_WEBHOOK_URL not set"}
    resp = httpx.post(settings.slack_webhook_url, json={"text": text}, timeout=20.0)
    ok = 200 <= resp.status_code < 300
    return {"sent": ok, "status_code": resp.status_code}


def artifact_to_slack_text(avatar_name: str, artifact: dict) -> str:
    """Render a post-meeting artifact as a Slack message."""
    lines = [f"*{avatar_name} — meeting follow-up*", artifact.get("summary", ""), ""]
    for c in artifact.get("checklist", []) or []:
        gap = c.get("gap_type", "none")
        tag = "" if gap in ("none", None) else f"  _[{gap}]_"
        lines.append(f"• {c.get('item','')} (owner: {c.get('owner','UNASSIGNED')}){tag}")
    return "\n".join(lines).strip()
