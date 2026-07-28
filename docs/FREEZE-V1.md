# Freezing v1 — keeping the standalone platform alive while the Cedric-Laura integration is built

**Decision (owner, 2026-07-28):** development moves to the Cedric-Laura
platform. This version stays a **working product**, not an archive — the team
keeps using and demoing it until the other one is finished.

**Frozen ref:** tag `v1-frozen-2026-07-28b` = branch `frozen/v1` = `433a4fb`.

> **APPLIED 2026-07-28 16:09 CEST.** App Runner `laura-backend` is pinned to
> `frozen/v1` (UpdateService `e6bb761ae6f944369c2fd790b0f286a1`, SUCCEEDED,
> service RUNNING). The change was one field out of 114 — `SourceCodeVersion.Value`
> `main` → `frozen/v1` — with all **71 env vars and 32 secrets** verified intact
> before and after. Auto-deploy stays ON so a deliberate hotfix pushed to
> `frozen/v1` still ships; nothing else pushes there.
>
> Users are now served `frozen/v1` and only that. `main` is free for the next
> platform. The commit that added this note is itself the proof: it landed on
> `main` and produced no deployment.

---

## The one thing that makes this urgent

**App Runner auto-deploys from `main`** (`AutoDeploymentsEnabled: true`,
`SourceCodeVersion: main`). So right now there is *no* freeze: the next push to
`main` for the new platform silently replaces the running product, mid-demo if
the timing is unlucky.

Everything below exists to close that, and the four quieter versions of it.

---

## What is shared, and how each one breaks

| # | Shared thing | How the new platform breaks it | Blast radius |
|---|---|---|---|
| 1 | **`main` branch** | any push → auto-deploy | **prod backend replaced** |
| 2 | **ElevenLabs agents** `agent_9601…` (Laura), `agent_0801…` (Cedric) | a `create_meeting_agent.py` run re-writes the SAME ids | both avatars change persona/tools mid-meeting |
| 3 | **Cloudflare worker `cedric-voice`** | `wrangler deploy` replaces the bridge for everyone | turn-taking changes or the bridge dies → avatars mute |
| 4 | **Litestream replica** `s3://laura-org-memory/store` | two backends replicating to one prefix | **store corruption** — the worst one |
| 5 | SSM secrets `/laura/prod/*` | read-only sharing | fine, leave shared |

1–4 must be separated. 5 does not.

---

## The plan

### Step 1 — restore point ✅ DONE

```
tag    v1-frozen-2026-07-28   (annotated, immutable)
branch frozen/v1              (same commit)
```

Nothing can lose this version now. Everything else is about not *replacing* it.

### Step 2 — pin prod to the frozen ref ← **the one that matters**

Point App Runner at `frozen/v1` instead of `main`. Auto-deploy stays ON: nobody
pushes to `frozen/v1`, so it never redeploys — but a deliberate hotfix still can
(push to `frozen/v1` → deploys). `main` becomes free for the new platform with
zero effect on the running product.

```bash
aws apprunner update-service --region eu-central-1 \
  --service-arn arn:aws:apprunner:eu-central-1:836739852304:service/laura-backend/f169c4a486cd47bfac9736ab01367a26 \
  --source-configuration file://frozen-source-config.json
```

> Use `--source-configuration file://…` — inline JSON has bitten this service
> before (see `laura-pipedream-production-flip`). Take the current config from
> `describe-service`, change only `SourceCodeVersion.Value`, and **keep the full
> env map**: `update-service` REPLACES it, and a partial map silently drops
> variables (this has caused an outage here before).

Do it with `active_sessions == 0`.

### Step 3 — separate the shared runtime pieces *(only when the new platform first runs)*

Not needed today. Needed the day the new platform boots against real vendors.

- **ElevenLabs** — the new platform must create its OWN agents.
  `create_meeting_agent.py` keys idempotency by name (`"{display} Meeting
  Pilot"`), so a **different avatar display name gives a different agent**.
  Same name = it overwrites the frozen agents. This is the easiest one to get
  wrong, because it fails silently and only shows up in a live meeting.
- **Cloudflare** — new worker name (e.g. `cedric-voice-v2`), and point the new
  backend's `VOICE_AGENT_RELAY_WS_BASE` at it. Never redeploy `cedric-voice`
  from the new tree.
- **Litestream** — a different `LITESTREAM_REPLICA_URL` prefix
  (`s3://laura-org-memory/store-v2`). Two writers on one prefix corrupts both.
- **App Runner** — a second service for the new platform; leave `laura-backend`
  on `frozen/v1`.

### Step 4 — how to hotfix the frozen product

It is a product, so it will need fixes.

```bash
git checkout frozen/v1
git checkout -b fix/whatever-broke
# … change, test: pytest backend/tests -q  AND  node --test "relay/cedric-voice/test/*.test.mjs"
gh pr create --base frozen/v1
# merge → App Runner auto-deploys frozen/v1
git tag -a v1-frozen-<date> -m "…"   # new restore point
```

Rule: **fixes land on `frozen/v1` first.** Cherry-pick forward to `main` if the
new platform wants them. Never the reverse — `main` will drift, and a
merge back from it drags the whole new architecture into the frozen product.

### Step 5 — restoring, if the new platform is abandoned or delayed

Repoint App Runner back to `main`, or merge `frozen/v1` into `main`. The tag
means this is always possible, whatever `main` has become.

---

## Verifying the freeze actually holds

After Step 2, one push to `main` should change **nothing**:

```bash
curl -s https://dhfgfe6yw6.eu-central-1.awsapprunner.com/health   # unchanged
aws apprunner list-operations --region eu-central-1 --service-arn <arn> --max-results 1
# → no new START_DEPLOYMENT
```

Worth doing deliberately once, with a trivial commit, rather than discovering it
during a customer call.

---

## What "frozen" does NOT protect against

Honest limits — none of these are in our git history:

- **Vendor drift.** ElevenLabs, Recall, Deepgram, Anthropic all change under us.
  A frozen commit is not a frozen product; the Recall 507 outage on 2026-07-28
  is the recent example.
- **The ElevenLabs agents themselves** — their config lives in ElevenLabs, not
  in the repo. `create_meeting_agent.py --dry-run` shows what the repo *thinks*;
  the live agent can drift from it (and did: `built_in_tools` vs `tools`).
- **Credential expiry.** Google OAuth in testing mode expires; Recall and
  ElevenLabs keys can be rotated. A frozen deploy with a dead credential is a
  dead product.
- **The Cloudflare account split.** `cedric-voice` lives in the
  `Duccioprofeti@gmail.com` account, not `duccio@sffstudio.com` — a redeploy
  needs that login. Worth putting a Workers-scoped token in SSM
  (`/laura/ops/CF_WORKERS_TOKEN`) so this is not a one-person dependency.
