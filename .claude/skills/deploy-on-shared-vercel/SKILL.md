---
name: deploy-on-shared-vercel
description: Wire up continuous deployment for a GitHub repo on a SHARED / Pro Vercel team so that every push to the production branch deploys, regardless of who authored the commit (teammates, bots, unverified emails), by creating a DEDICATED new Vercel project, a GitHub Actions workflow that deploys via the Vercel CLI (owner-attributed, so it bypasses Vercel's commit-author block), the token/secret wiring, and an optional custom domain assumed to be on Cloudflare. Use when the user wants to "set up Vercel deploys", "auto-deploy on push", "deploy this repo to our Vercel team", "connect a domain", or reports that Vercel deployments are BLOCKED because a commit author isn't a team member.
allowed-tools: Bash, Read, Write, Edit
user-invocable: true
---

# Deploy on a shared Vercel team (any committer) + Cloudflare domain

## Why this skill exists: the core problem

On a **Pro/Team** Vercel account, Vercel **blocks every git-sourced deployment whose commit author's email is not a verified member of the Vercel team.** This applies to:

- the native Git integration (push → deploy),
- **deploy hooks** (they are still git-sourced: they do NOT bypass the check),
- `vercel deploy` run inside a checkout that still has a `.git` directory (it reads the HEAD commit author).

So if commits come from teammates, bots, or emails not attached to a GitHub account (e.g. `someone@university.edu`), those deploys land as **BLOCKED** and production never updates. The error looks like:

> *The deployment was blocked because the commit email X could not be matched to a GitHub account.*

**The only reliable fix for "any committer":** deploy via the **Vercel CLI with a token, from a directory with the git metadata removed**, so the deployment is attributed to the **token owner** instead of the commit author. A GitHub Action does this on every push. This works for literally any committer, including bots.

## Two hard rules (learned the hard way)

1. **NEVER reuse an existing Vercel project.** Always create a NEW, dedicated project for the repo. Reusing a project that already runs another app (it will have that app's env vars / domains / deployments) risks clobbering a live app and commingling deployments. Before creating, check whether a same-named project already exists and confirm it is not a live app.
2. **The agent usually CANNOT write GitHub secrets or edit permission settings** in auto mode; a safety classifier hard-blocks `gh secret set` and self-granting permission rules. Plan for the user to add the one secret (`VERCEL_TOKEN`) themselves, OR to add a `Bash(gh secret set:*)` allow rule first. Put the non-secret IDs directly in the workflow so only ONE secret is ever needed.

## Keep questions light: prefer defaults over asking

Don't interrogate the user. Resolve what you can yourself (`gh auth status`, `vercel teams ls`, existing project list, repo framework detection) and lean on these defaults, noting assumptions in a line rather than asking:

- **Project name** → `<repo>-site` (or `<repo>-landing` for a marketing repo).
- **Production branch** → the repo's default branch.
- **Framework** → auto-detect from `package.json`.
- **Team/scope** → if there is exactly one Vercel team, use it; ask if several exist.
- **`www` handling** → 308 redirect to apex.
- **Token** → for the GitHub secret, a **dedicated dashboard token is strongly preferred**: the CLI login token ROTATES (sometimes within a day), silently breaking the Action with "token is not valid". The CLI token is fine for the agent's own API calls during setup.

Questions are fine for genuinely consequential unknowns, typically the **domain** (which one, apex vs subdomain) or an ambiguous team, but bundle them together rather than asking serially, and mark a recommended default. Skip questions whose answer has an obvious conventional default.

## Inputs to gather first

- **Repo**: `OWNER/REPO` on GitHub (must be pushable; `gh auth status` OK).
- **Vercel scope/team**: get the team id: `vercel teams ls` or `GET https://api.vercel.com/v2/teams`. It looks like `team_xxx`. Confirm the plan is Pro (`GET /v2/teams/{id}` → `billing.plan`).
- **New project name**: e.g. `<repo>-landing` / `<repo>-site`: something that is NOT an existing project.
- **Production branch**: usually `main`.
- **Domain** (optional): apex like `example.ai` and/or subdomain like `app.example.ai`, assumed registered on **Cloudflare**.
- **Vercel token**: read the CLI token from disk for API calls (see below). For the GitHub Action, prefer a **dedicated dashboard token**; the CLI login token also works.

```bash
# Vercel CLI token on disk (Windows path shown; macOS/Linux: ~/.local/share/com.vercel.cli/auth.json or ~/Library/...)
TOKEN=$(python -c "import json;print(json.load(open('C:/Users/<you>/AppData/Roaming/com.vercel.cli/Data/auth.json'))['token'])")
```

All API calls below use `Authorization: Bearer $TOKEN` and `?teamId=$T` where `T=team_xxx`.

## Step 1: Create a NEW dedicated Vercel project (git-linked)

```bash
NEW=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://api.vercel.com/v11/projects?teamId=$T" \
  -d '{"name":"<project-name>","framework":"nextjs","gitRepository":{"type":"github","repo":"OWNER/REPO"}}' \
  | python -c "import sys,json;d=json.load(sys.stdin);print(d.get('id') or json.dumps(d.get('error')))")
echo "$NEW"   # prj_...
```

- Set `framework` to the real framework (`nextjs`, `vite`, `astro`, or `null` to auto-detect).
- Git-linking requires the account to have a GitHub **Login Connection** in Vercel (Account → Settings → Login Connections / the GitHub app installed on the org). If `vercel git connect` / linking errors with *"add a Login Connection"*, the user must connect GitHub once in the Vercel dashboard (browser step); it cannot be done headlessly.
- Copy any required **environment variables** into the new project (`POST /v10/projects/{id}/env`); the deploy pulls prod env from project settings.

## Step 2: Disable native git deploys entirely (avoid duplicate/blocked deploys)

Add `vercel.json` at the repo root disabling the native Git integration for ALL branches; the Action owns both production and previews:

```json
{
  "git": {
    "deploymentEnabled": false
  }
}
```

Disabling only `main` is not enough: native **PR preview** deploys still run and show a red ❌ check ("No GitHub account was found matching the commit author email address") on any PR from an unverified author.

## Step 3, Add the GitHub Actions workflow (production + PR previews)

`.github/workflows/deploy.yml`, both jobs strip git metadata so the deploy is attributed to the token owner (bypasses the commit-author block), then deploy via the CLI. **Hardcode the non-secret org/project IDs** so `VERCEL_TOKEN` is the only secret:

```yaml
name: Deploy to Vercel

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch: {}

env:
  VERCEL_ORG_ID: team_xxx          # non-secret identifier
  VERCEL_PROJECT_ID: prj_xxx       # non-secret identifier (the NEW project)

jobs:
  production:
    if: github.event_name == 'push' || github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    concurrency:
      group: vercel-production-deploy
      cancel-in-progress: false
    env:
      VERCEL_TOKEN: ${{ secrets.VERCEL_TOKEN }}
    steps:
      - uses: actions/checkout@v4
      - name: Strip git metadata            # owner-attributed -> any committer deploys
        run: rm -rf .git
      - run: npm install --global vercel@latest
      - name: Deploy to production
        run: |
          if [ -z "$VERCEL_TOKEN" ]; then
            echo "::warning::VERCEL_TOKEN not set yet; skipping deploy."
            exit 0
          fi
          vercel deploy --prod --yes --token="$VERCEL_TOKEN"

  preview:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    concurrency:
      group: vercel-preview-${{ github.event.pull_request.number }}
      cancel-in-progress: true
    env:
      VERCEL_TOKEN: ${{ secrets.VERCEL_TOKEN }}
      GH_TOKEN: ${{ github.token }}
    steps:
      - uses: actions/checkout@v4
      - run: rm -rf .git
      - run: npm install --global vercel@latest
      - name: Deploy preview
        id: deploy
        run: |
          if [ -z "$VERCEL_TOKEN" ]; then
            echo "::warning::VERCEL_TOKEN unavailable (fork PR or secret unset); skipping preview."
            echo "url=" >> "$GITHUB_OUTPUT"
            exit 0
          fi
          URL=$(vercel deploy --yes --token="$VERCEL_TOKEN")
          echo "url=$URL" >> "$GITHUB_OUTPUT"
      - name: Comment preview URL on PR
        if: steps.deploy.outputs.url != ''
        run: |
          BODY="🔍 **Vercel preview:** ${{ steps.deploy.outputs.url }}"
          gh pr comment "${{ github.event.pull_request.number }}" --repo "${{ github.repository }}" --edit-last --body "$BODY" \
            || gh pr comment "${{ github.event.pull_request.number }}" --repo "${{ github.repository }}" --body "$BODY"
```

The `if [ -z ... ]` guards keep runs green before the secret exists and on fork PRs (which don't receive secrets).

## Step 4: The token + secret

- **Cannot mint a token from the CLI's OAuth token** (`POST /v3/user/tokens` → `Cannot create tokens for this app`). Have the user create a dedicated token at **Vercel → Account → Settings → Tokens** (scope to the team). Verify a token works: `vercel whoami --token="$TOKEN"`.
- ⚠️ **Do NOT put the CLI login token in the GitHub secret** except as a stopgap: it rotates (sometimes within a day) and the Action then fails with *"The token provided via --token argument is not valid"*. If that error appears later, the secret is stale; replace it with a dedicated token.
- **Set the one secret.** Smoothest flow that works out of the box: ask the user to create the dedicated token in the dashboard (open `https://vercel.com/account/settings/tokens` for them; name it `github-actions-<repo>`, no expiration) and **paste the token into the chat**. Once the user has explicitly handed over the token, the agent can run `gh secret set` itself; the auto-mode classifier allows it (it blocks the agent inventing/harvesting a secret, not storing one the user provided):

  ```bash
  vercel whoami --token="$PASTED"                     # verify BEFORE storing
  printf '%s' "$PASTED" | gh secret set VERCEL_TOKEN --repo OWNER/REPO
  gh secret list --repo OWNER/REPO                    # confirm
  ```

  Remind the user the token passed through the chat transcript; rotate it if the transcript is ever shared. Fallback if the secret write is still blocked: give the user the PowerShell one-liner `gh secret set VERCEL_TOKEN --repo OWNER/REPO --body "PASTE"`.

## Step 5: Commit, push, verify

```bash
git add .github/workflows/deploy.yml vercel.json && git commit -m "ci: auto-deploy to Vercel (any committer)" && git push origin main
gh workflow run deploy.yml --repo OWNER/REPO --ref main       # manual trigger
gh run list --repo OWNER/REPO --limit 1
```

For an **immediate** first production deploy (before/independent of the Action), deploy locally with git metadata removed so it is owner-attributed:

```bash
cp -r <checkout> /tmp/deploysrc && rm -rf /tmp/deploysrc/.git
mkdir -p /tmp/deploysrc/.vercel
echo '{"projectId":"'$NEW'","orgId":"'$T'"}' > /tmp/deploysrc/.vercel/project.json
cd /tmp/deploysrc && vercel --prod --yes --token="$TOKEN"
```

Confirm the deployment is `READY` (not `BLOCKED`):
```bash
curl -s -H "Authorization: Bearer $TOKEN" "https://api.vercel.com/v6/deployments?projectId=$NEW&teamId=$T&limit=1&target=production" \
  | python -c "import sys,json;x=json.load(sys.stdin)['deployments'][0];print(x['readyState'], x['url'])"
```

## Recovering from failed deploys (stale token, outage, missed pushes)

If deploys failed for a while (e.g. the token went stale), commits landed on the branch **without reaching production**. Two traps when recovering:

1. **`gh run rerun <id> --failed` redeploys THAT RUN'S commit, not HEAD.** If newer commits (e.g. a teammate's merge) failed after the run you rerun, production ends up silently BEHIND main. After fixing the cause, deploy HEAD explicitly:
   ```bash
   gh workflow run deploy.yml --repo OWNER/REPO --ref main   # workflow_dispatch always deploys current HEAD
   ```
   (Rerunning the newest failed run also works; just make sure it's the newest.)
2. **Always verify production matches HEAD afterward**: compare content, not just run status:
   ```bash
   gh api repos/OWNER/REPO/commits/main --jq '.sha[0:8]'      # what should be live
   gh run list --repo OWNER/REPO --limit 3                    # all green?
   curl -s https://<domain>/<page-a-recent-commit-changed> | grep -i "<something that commit added>"
   ```

Diagnosis shortcut: `"The token provided via --token argument is not valid"` in the run log = stale token → replace the secret (Step 4), then deploy HEAD as above.

## Step 6: Custom domain on Cloudflare

**Vercel side (agent, via API):** add the domain(s) to the project.

```bash
# apex
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://api.vercel.com/v10/projects/$NEW/domains?teamId=$T" -d '{"name":"example.ai"}'
# www -> 308 redirect to apex
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://api.vercel.com/v10/projects/$NEW/domains?teamId=$T" -d '{"name":"www.example.ai","redirect":"example.ai","redirectStatusCode":308}'
# a subdomain (e.g. studio/app)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://api.vercel.com/v10/projects/$NEW/domains?teamId=$T" -d '{"name":"app.example.ai"}'
```

Read the exact records Vercel wants:
```bash
curl -s -H "Authorization: Bearer $TOKEN" "https://api.vercel.com/v6/domains/example.ai/config?teamId=$T" \
  | python -c "import sys,json;d=json.load(sys.stdin);print('misconfigured',d.get('misconfigured'));print('A',d.get('recommendedIPv4'));print('CNAME',d.get('recommendedCNAME'))"
```

**Cloudflare side (BROWSER; give the user these instructions):** in Cloudflare → the zone → **DNS → Records**, add and set **Proxy status = DNS only (grey cloud)** on each:

| Kind | Type | Name | Value |
|------|------|------|-------|
| apex | A | `@` | `216.150.1.1` **and** `216.150.16.1` (Vercel's current pair; a single `76.76.21.21` also works) |
| www | CNAME | `www` | `cname.vercel-dns.com` |
| subdomain | CNAME | `app` (or `studio`) | `cname.vercel-dns.com` |

> 🔴 **Grey cloud (DNS only), NOT orange/proxied.** Vercel provisions its own HTTPS cert and validates the record directly; the Cloudflare proxy causes cert-validation failures and redirect loops. Cloudflare will show "not proxied / cannot reach" warnings. **expected and ignorable** in this setup. To proxy later, first set Cloudflare **SSL/TLS → Full (strict)**.
> Moving a domain between Vercel projects needs **no DNS change**: just re-assign it in Vercel (`DELETE` from old project, `POST` to new); Vercel routes by assignment, not IP.

**Verify** after the user adds the records:
```bash
curl -s -H "Authorization: Bearer $TOKEN" "https://api.vercel.com/v6/domains/example.ai/config?teamId=$T" | python -c "import sys,json;print('misconfigured',json.load(sys.stdin).get('misconfigured'))"
curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" https://example.ai
echo | openssl s_client -connect example.ai:443 -servername example.ai 2>/dev/null | openssl x509 -noout -issuer   # Let's Encrypt = cert issued
```
`misconfigured:false` + a valid cert = live. The apex cert usually lands in under a minute; a `www`/subdomain cert can lag a few minutes (`curl` may return `000` until it does).

## Pitfalls checklist

- ❌ Reused an existing project → clobbered/commingled a live app. **Always make a new project.**
- ❌ Relied on the native Git integration or a **deploy hook** for "any committer" → BLOCKED. Only the CLI-with-token + `rm -rf .git` path is owner-attributed.
- ❌ Left `.git` in the CI checkout → deploy attributed to the commit author → BLOCKED. Always `rm -rf .git` before `vercel deploy`.
- ❌ Cloudflare record left **orange/proxied** → cert/redirect-loop failures. Must be grey (DNS only).
- ❌ Tried to `gh secret set` from the agent in auto mode → hard-blocked. Hand the one secret to the user (or have them add a permission rule first).
- ❌ Forgot `vercel.json` `"deploymentEnabled": false` → native + Action both fire (duplicate builds, blocked native deploys cluttering the dashboard, red ❌ preview checks on PRs from unverified authors).
- ❌ Put the rotating CLI login token in the GitHub secret → Action breaks within days with "token is not valid". Use a dedicated dashboard token.
- ❌ After fixing a broken token, reran an OLD failed run → production silently behind main (a teammate's later merge never deployed). Deploy HEAD via `workflow_dispatch` and verify live content matches main.
