# Storage durability — rollout (owner runbook)

**What this delivers:** Laura's org memory (ledger + artifacts + live session
state, one SQLite file) survives App Runner deploys instead of being wiped on
every push to `main`. See [`STORAGE-DURABILITY.md`](STORAGE-DURABILITY.md) for
the full decision.

**Chosen variant: (b) source deploy + boot-time Litestream** — keep the existing
managed `PYTHON_311` source deploy of `laura-backend`; a boot wrapper
([`scripts/start-with-litestream.sh`](../../scripts/start-with-litestream.sh))
fetches the Litestream binary, restores the DB from S3, then runs `uvicorn`
under `litestream replicate`. No image/ECR migration, no CI changes. The wrapper
is **fail-open**: if S3/binary/restore is unavailable it boots `uvicorn`
directly (exactly today's behaviour), so this can never wedge the live path.

> **This PR contains code only.** Nothing here runs any AWS command or touches
> the running service. The steps below are the manual work **you** do to turn
> durability on. Every step is reversible (rollback at the bottom).

---

## What Claude already did (in this PR)

- `etc/litestream.yml` — replica config (S3 URL, region, 10s sync, 7-day retention), fully env-driven.
- `scripts/start-with-litestream.sh` — restore-on-boot → `replicate -exec uvicorn`, fail-open.
- This runbook.
- **No** change to `store.py`, `ledger.py`, `config.py`, the Dockerfile, or any test. Zero Python delta.

## What you do (≈15 min, in order)

### 1 — Create the S3 bucket (eu-central-1, private)

Same region as Recall for data residency. Bucket name used below:
`laura-org-memory` (pick your own; keep it consistent).

```bash
aws s3api create-bucket \
  --bucket laura-org-memory \
  --region eu-central-1 \
  --create-bucket-configuration LocationConstraint=eu-central-1

# Block all public access
aws s3api put-public-access-block \
  --bucket laura-org-memory \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Default server-side encryption (SSE-S3)
aws s3api put-bucket-encryption \
  --bucket laura-org-memory \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

**PII note:** while a meeting is live the replica contains transcript
utterances. Those rows are deleted at finalize (`store.remove`, CASCADE), so the
*durable* tail is distilled-only — but add a short lifecycle rule so any live
tail can't linger. Litestream `retention: 168h` already prunes its own history;
this lifecycle rule is the belt-and-suspenders on incomplete multipart uploads
and any stray objects:

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket laura-org-memory \
  --lifecycle-configuration '{"Rules":[
    {"ID":"expire-noncurrent","Status":"Enabled","Filter":{"Prefix":""},
     "NoncurrentVersionExpiration":{"NoncurrentDays":7},
     "AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}'
```

### 2 — Attach an S3 IAM policy to the existing instance role

Litestream reads AWS credentials from the App Runner **instance role**
(`LauraAppRunnerInstanceRole`) automatically — **no access keys, no new
secrets.** Attach this minimal inline policy (least privilege, this bucket only):

```bash
aws iam put-role-policy \
  --role-name LauraAppRunnerInstanceRole \
  --policy-name LitestreamOrgMemoryS3 \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "LitestreamListBucket",
        "Effect": "Allow",
        "Action": ["s3:ListBucket"],
        "Resource": "arn:aws:s3:::laura-org-memory"
      },
      {
        "Sid": "LitestreamObjectRW",
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        "Resource": "arn:aws:s3:::laura-org-memory/*"
      }
    ]
  }'
```

> If `LauraAppRunnerInstanceRole` isn't the *instance* role (App Runner has two
> roles — an **access** role for ECR and an **instance** role for app AWS calls),
> confirm with `aws apprunner describe-service --service-arn <arn> \
> --query 'Service.InstanceConfiguration.InstanceRoleArn'`. The policy goes on
> the **instance** role.

### 3 — Point the service at the wrapper + set env vars

This is a config update to the **same source-based service** — *not* a
service-type change. In the App Runner console (or via `update-service`):

- **Start command:** change from
  `python3 -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000`
  to
  `bash scripts/start-with-litestream.sh`
- **Runtime environment variables** — add:

  | Key | Value | Notes |
  |---|---|---|
  | `LITESTREAM_REPLICA_URL` | `s3://laura-org-memory/store` | **the on/off switch** — the wrapper only replicates when this is set |
  | `LAURA_STORE_PATH` | `/tmp/laura/store.sqlite3` | stable writable path the app + Litestream share (any writable path works; it's ephemeral by design) |
  | `LITESTREAM_REGION` | `eu-central-1` | optional (this is the default) |
  | `LITESTREAM_SYNC_INTERVAL` | `10s` | optional (default) — ≤10s tail loss on a hard crash |

  Leave secrets (Recall/Anam/etc.) exactly as they are — carried over verbatim.

Do this in a **no-meeting window** (a deploy restarts the instance).

CLI equivalent (fill in your service ARN; this reuses the existing source
config and only overrides StartCommand + env — verify the JSON against your
current `describe-service` output before running):

```bash
aws apprunner update-service \
  --service-arn <LAURA_BACKEND_SERVICE_ARN> \
  --source-configuration '{
    "AutoDeploymentsEnabled": true,
    "CodeRepository": {
      "CodeConfiguration": {
        "ConfigurationSource": "API",
        "CodeConfigurationValues": {
          "Runtime": "PYTHON_311",
          "BuildCommand": "pip install -r requirements.txt",
          "StartCommand": "bash scripts/start-with-litestream.sh",
          "Port": "8000",
          "RuntimeEnvironmentVariables": {
            "LITESTREAM_REPLICA_URL": "s3://laura-org-memory/store",
            "LAURA_STORE_PATH": "/tmp/laura/store.sqlite3",
            "LITESTREAM_REGION": "eu-central-1",
            "LITESTREAM_SYNC_INTERVAL": "10s"
          }
        }
      }
    }
  }'
```

> Copy your CURRENT `BuildCommand`, `Runtime`, `Port`, and the full existing env
> block out of `aws apprunner describe-service` first and merge — the snippet
> above shows only the fields that change, and `update-service` replaces the
> whole `SourceConfiguration`.

### 4 — Verify durability end-to-end (the real acceptance test)

1. After the deploy, tail the App Runner logs and confirm the wrapper engaged:
   `[start-with-litestream] using litestream at …` and Litestream lines about
   replicating — **not** `durability OFF`.
2. Run a real (or stub) session; confirm ledger rows via `GET /org/actions`.
3. Push a trivial commit to force a redeploy (or hit "Deploy" in the console).
4. After the new instance is up, hit `GET /org/actions` again → **the same rows
   are still there.** That's the whole point.
5. Confirm the S3 bucket now has objects under `store/`
   (`aws s3 ls s3://laura-org-memory/store/ --recursive`).

---

## Rollback (instant, reversible)

- **Turn replication off, keep the wrapper:** delete the `LITESTREAM_REPLICA_URL`
  env var → next boot the wrapper runs `uvicorn` directly (today's behaviour).
- **Full revert:** set the Start command back to
  `python3 -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000`.
- Worst case the app boots with an empty DB — identical to today's post-deploy
  state. Nothing is destroyed; the S3 copy remains for a later retry.

---

## Hard constraints (do not violate)

- **Stay pinned to one instance.** Autoscaling `LauraCostControl` is
  MinSize=1 / MaxSize=1. Two writers = two Litestream generations fighting =
  corruption. The app itself already requires a single instance (in-memory
  sessions / ws routing), so this is not a new limit — but never raise MaxSize
  while on Litestream. Horizontal scale is the "move to Postgres" moment
  (see decision doc §2b).
- **Don't deploy mid-meeting.** A rolling deploy has a brief dual-writer window;
  writes to the old instance after the new one restores are lost (seconds).
  Deploy in quiet windows — already informal practice, and strictly better than
  today (deploys already kill in-flight sessions).

## If you'd rather do variant (a) later (image/ECR)

The decision doc recommends the image path long-term (converges with the GPU
container track). To switch: install `litestream` into the root `Dockerfile`,
set its `CMD` to `["bash","scripts/start-with-litestream.sh"]` (the same wrapper
works unchanged — it uses `litestream` from PATH if present and skips the
download), stand up an ECR repo + a build-and-push GitHub Action, then
`update-service` to image-based. Everything in this PR is forward-compatible
with that; only the deploy mechanism changes.
