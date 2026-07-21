# Jira Cloud connector

Petra reads the org's **open issues** (owners, due dates) into her meeting brief
at join; the same shape as the Asana connector (`backend/app/jira_client.py`).
Two ways to connect, both self-serve from the **Connections** tab:

## 1. One-click OAuth ("Connect Jira" → Atlassian login → connected)

The nice flow. It needs a **one-time Atlassian OAuth app** (its client-id/secret
live in the server env; that's inherent to OAuth, exactly like the Google and
Asana connect buttons). Once configured, the card shows a single **Connect Jira**
button that bounces the user to Atlassian and back, connected; no tokens to paste.

**Set it up once:**
1. https://developer.atlassian.com/console/myapps/ → **Create** → **OAuth 2.0 integration**.
2. **Permissions** → add **Jira API** with scopes: `read:jira-work`, `read:jira-user`, `offline_access` (offline_access is required; it mints the refresh token).
3. **Authorization** → **OAuth 2.0 (3LO)** → **Callback URL**: `{PUBLIC_BASE_URL}/oauth/jira/callback` (prod: `https://<host>/oauth/jira/callback`).
4. **Settings** → copy the **Client ID** and **Secret**, and set on the deployment:
   ```
   JIRA_CLIENT_ID=<client id>
   JIRA_CLIENT_SECRET=<secret>
   ```
5. Redeploy. The Connections card's Jira tile now shows the one-click **Connect Jira** redirect.

Reads then target `https://api.atlassian.com/ex/jira/{cloudid}/rest/api/3` with a
minted Bearer token; the grant (refresh token + cloud id) is stored encrypted
per-org (`org_oauth`, provider="jira-oauth").

## 2. API-token fallback (no deploy credentials)

Until the Atlassian app above exists, the card shows a paste-a-token form so
teams can connect immediately:
- **Site URL** (`https://yourco.atlassian.net`), account **email**, and a **Jira API token**
  (Jira → Account settings → Security → **Create and manage API tokens**).
- Verified live (`/myself`) and stored encrypted per-org (provider="jira").
- Auth is HTTP Basic `email:token` against `{site}/rest/api/3`.

## Notes
- Read-only for now (open-issues snapshot at join). Writing issues from approved
  actions is a later step, mirroring the Asana executor.
- Env fallback (`JIRA_SITE` / `JIRA_EMAIL` / `JIRA_API_TOKEN`) covers single-tenant.
- Everything is best-effort and PII-safe: no issue descriptions, no tokens logged.
