# OpenClaw gateway on AWS — development deployment runbook

A secure, repeatable **development** deployment of the OpenClaw gateway that
replaces the current *Mac + ephemeral `trycloudflare` tunnel*. It is scoped to a
**single Laura development test organization**. It is **not** a hostile
multi-tenant service — see [Not multi-tenant](#not-multi-tenant).

Everything here is infrastructure-as-code under
[`infra/openclaw-gateway/`](../infra/openclaw-gateway/). This document is the
operator runbook. The commands that touch AWS are written for a human to run
against a **dev account**; nothing in this repo runs them for you.

- **Contract preserved:** Laura still talks to the gateway exactly as today —
  `POST <OPENCLAW_GATEWAY_URL>/v1/responses` with `Authorization: Bearer
  <token>` and the `x-openclaw-agent-id` / `x-openclaw-session-key` headers
  (`backend/app/openclaw/runtime.py`). This work changes only *where the gateway
  runs and how it is secured*, never the backend integration.
- **Experiment stays OFF by default.** With `OPENCLAW_EXPERIMENT_ENABLED=false`
  (the code default) nothing routes to a gateway. No backend code is modified.

---

## What the gateway is, and what it needs

OpenClaw is an open-source agent gateway. Laura points at its OpenAI-compatible
`/v1/responses` endpoint and does **client-side function calling**: the gateway's
model proposes tool calls, and Laura executes them in its own backend behind the
approval door. So the gateway needs an LLM key of its own and must be reachable
by Laura over authenticated TLS — but it does **not** need to call back into
Laura.

Facts pinned from the official OpenClaw docs (`docs.openclaw.ai`, `ghcr.io/openclaw/openclaw`):

| Property | Value |
|---|---|
| Official image | `ghcr.io/openclaw/openclaw` (GHCR) or `openclaw/openclaw` (Docker Hub mirror). **Avoid unofficial mirrors.** |
| Version tags | date-based releases and prereleases. Resolve a reviewed release to an immutable multi-arch digest; **never deploy `latest` / `main`.** |
| Port | `18789` (gateway API + Control UI) |
| Health | `/healthz` (liveness), `/readyz` (readiness) |
| Auth | `OPENCLAW_GATEWAY_TOKEN` bearer token |
| Persistent state | `/home/node/.openclaw` (config + workspace), `/home/node/.config/openclaw` (auth-profile **secret keys**) |
| Official hardening | keep the gateway on loopback, terminate **TLS at an authenticated reverse proxy**, drop `NET_RAW`/`NET_ADMIN`, `no-new-privileges`, authenticate `/metrics`. "Publicly exposed agent gateways become scanning targets within hours." |

Because the gateway holds **secret key material on a local disk** and is designed
around a persistent workspace, it is a **stateful** service — that single fact
drives the architecture choice below.

---

## Chosen architecture

**One always-on ARM EC2 instance running Docker Compose: the pinned OpenClaw
image on an internal-only Docker network, behind a Caddy TLS reverse proxy, with
a persistent encrypted EBS data volume.** Secrets live in SSM SecureString and
are pulled at boot into a root-only `0600` env file. Admin is via SSM Session
Manager (no SSH, no key pair). Logs ship to CloudWatch with retention. This
mirrors OpenClaw's own recommended topology (gateway on loopback, TLS at a
proxy, on hardware you control).

```
Laura backend (laura-backend-next, App Runner, eu-central-1)
        │  HTTPS  POST /v1/responses   Authorization: Bearer <token>
        ▼
 [ Front door ]  ── recommended: Cloudflare named tunnel (no inbound port)
                └─ alt:         Caddy + Route53 dev subdomain (Let's Encrypt)
        │  (TLS terminates here; bearer still enforced by OpenClaw)
        ▼  compose network; 18789 is expose-only (never host-published)
 openclaw-gateway :18789   ──►  /opt/openclaw/data (EBS gp3, encrypted)
  ANTHROPIC_API_KEY, OPENCLAW_GATEWAY_TOKEN  (SSM SecureString → 0600 env)
```

The bootstrap writes and validates a narrow OpenClaw runtime configuration:

- `gateway.mode=local`, `gateway.bind=lan`, and token auth with failed-auth
  rate limiting;
- `gateway.http.endpoints.responses.enabled=true` (the endpoint is disabled by
  OpenClaw by default);
- primary model `anthropic/claude-opus-5`;
- Control UI, host terminal, elevated tools, browser runtime, and mDNS
  discovery disabled;
- built-in tools restricted to the `minimal` profile. Laura supplies
  organization-scoped connected-app function tools in each authenticated
  request and still owns approval and execution;
- metadata-only audit (`logging.audit.messages=off`) and enforced seven-day
  session retention with bounded entry and disk limits.

### Why EC2 + Docker + EBS (and not the alternatives)

| Option | Verdict | Why |
|---|---|---|
| **EC2 + Docker + persistent EBS** | **Chosen** | Only option that cleanly gives durable local disk for the auth-profile **secret keys** + workspace, a co-located authenticated reverse proxy, IMDSv2, SSM-only admin, and (if ever needed) the Docker-socket sandbox. Cheapest and fastest to a safe MVP; matches upstream's own hardening guide. |
| App Runner | **Rejected** | No persistent disk (`.claude/CONTEXT.md`: "ephemeral on App Runner — no persistent disk"). OpenClaw would lose its auth-profile secret keys + workspace on every deploy/restart. No sidecar reverse proxy, no Docker socket. Wrong tool for a stateful agent gateway. |
| ECS Fargate + EFS | **Rejected for the POC** | Works, but heavier: needs an ALB (~$16–18/mo) for TLS+health, plus EFS. Fargate cannot use a Docker socket (no agent sandbox), and the auth-profile secret-key store on NFS is fragile. More moving parts and cost for a single-org POC with no scale-out need. Revisit if this graduates to always-on multi-instance. |

### Not multi-tenant

This is a **single development test org**. It deliberately omits everything a
hostile multi-tenant deployment requires:

- **One** shared `OPENCLAW_GATEWAY_TOKEN` and **one** agent id — no per-tenant
  tokens, no per-tenant agents, no tenant-scoped rate limits or quotas.
- **One** shared workspace and auth store on one box — no per-tenant isolation
  of the agent's execution or state, no blast-radius containment between tenants.
- The trust model assumes **one trusted caller** (Laura's dev backend). Tool
  execution safety leans on Laura's approval door, not on sandboxing untrusted
  tenants.
- No network segmentation between tenants, no per-tenant audit separation.

A production multi-tenant gateway would need per-tenant tokens/agents,
per-tenant sandboxed execution (Docker-socket sandbox or microVM), network
isolation, quotas, and separate audit trails. **Do not point more than the one
dev test org at this.**

---

## Files

```
infra/openclaw-gateway/
├── cloudformation/openclaw-gateway.yaml   # the stack (EC2, EBS, SG, IAM, logs, EIP, recovery alarm)
├── compose/
│   ├── docker-compose.yml                 # gateway (internal-only) + Caddy TLS proxy
│   ├── docker-compose.tunnel.yml          # Cloudflare-tunnel overlay (no inbound port)
│   └── Caddyfile                          # TLS + HSTS; reverse_proxy to gateway:18789
├── config/params.example.env              # NON-SECRET params (copy to params.env, gitignored)
├── config/openclaw.json                   # managed narrow gateway config (Opus 5 + Responses API)
├── scripts/
│   ├── user-data.sh                       # EC2 bootstrap (IMDSv2, SSM secrets → 0600 env, compose up)
│   ├── deploy.sh                          # guard-first render+deploy of the stack
│   ├── dev_guard.py                       # dev-only guard: fails on customer resource names
│   ├── put-secrets.sh                     # write SecureString secrets to /laura/dev/openclaw/*
│   ├── smoke.sh                           # /healthz, /readyz, /v1/responses auth
│   ├── laura-backend-env-merge.sh         # safe describe→merge→update on laura-backend-next
│   ├── backup.sh / restore.sh             # EBS snapshot backup/restore
│   └── teardown.sh                        # guard-first delete-stack (snapshots the volume)
└── tests/                                 # key-free static validation (pytest or standalone)
```

---

## Human inputs required

These cannot be invented and must be supplied by an operator with dev-account access:

1. A **dev AWS account** + region (default `eu-central-1`), a VPC id, a **public
   subnet** id, and its AZ (`config/params.env`).
2. The **pinned OpenClaw image digest** — resolve and paste the full
   `ghcr.io/openclaw/openclaw:<version>@sha256:<digest>` reference.
3. Secret values, set via `put-secrets.sh` (never committed):
   `OPENCLAW_GATEWAY_TOKEN` (generate a strong random token) and the gateway's
   LLM key `ANTHROPIC_API_KEY`.
4. A **front door**: either a dev subdomain on a Route53 zone the team owns, or a
   Cloudflare named tunnel on a **dev** Cloudflare zone (see below). Do **not**
   use or point at the customer domain.

---

## Deploy

### 0) Pin the image digest

```bash
docker buildx imagetools inspect ghcr.io/openclaw/openclaw:<reviewed-version>
# put ghcr.io/openclaw/openclaw:<reviewed-version>@sha256:<digest> into config/params.env
```

The template's `OpenClawImage` `AllowedPattern` and `deploy.sh` both **refuse**
anything that is not `...@sha256:<64-hex>` — you cannot deploy `:latest`.

### 1) Secrets → SSM SecureString

```bash
export SECRETS_PATH_PREFIX=/laura/dev/openclaw AWS_REGION=eu-central-1
OPENCLAW_GATEWAY_TOKEN='<strong-random>' \
ANTHROPIC_API_KEY='<gateway-llm-key>' \
OPENCLAW_PUBLIC_HOSTNAME='openclaw-gw.dev.example.net' \
  infra/openclaw-gateway/scripts/put-secrets.sh
# For the tunnel path, also pass CLOUDFLARED_TOKEN=... (a SecureString).
```

> Caddy provisions the certificate from the hostname alone (no email required).
> To receive ACME expiry notices, add `email you@dev-zone` to the Caddyfile
> global options block.

Values come from the environment (not argv) so they stay out of shell history,
and are stored encrypted. The instance role can read **only** `/laura/dev/openclaw/*`.

### 2) Parameters

```bash
cp infra/openclaw-gateway/config/params.example.env infra/openclaw-gateway/config/params.env
# fill VpcId, SubnetId, AvailabilityZone, OpenClawImage
# FrontDoorMode=tunnel is the default; leave AllowedHttpsCidr closed
```

### 3) Deploy the stack

```bash
infra/openclaw-gateway/scripts/deploy.sh laura-openclaw-gw-dev
```

`deploy.sh` runs the **dev-only guard** on the stack name, enforces the pinned
digest, injects `user-data.sh` into the template, and runs
`aws cloudformation deploy`. It creates: the EC2 instance (IMDSv2 required, no
SSH key), a **closed-by-default** security group, a least-privilege instance
role, an encrypted gp3 data volume (`DeletionPolicy: Snapshot`), a CloudWatch log
group with retention, an Elastic IP, and an instance auto-recovery alarm.

### 4) Front door + TLS

Both paths give Laura a **publicly-trusted** HTTPS URL (Laura's `httpx` verifies
certificates — a self-signed / `tls internal` cert would fail, and disabling
verification is an out-of-scope backend change). Pick one.

**Recommended for this POC — Cloudflare named tunnel (zero inbound, no customer DNS).**
This is the durable, credentialed replacement for the ephemeral `trycloudflare`
URL, and it sidesteps two real problems the direct-443 path has (below).

1. Create a **named** tunnel on a **dev** Cloudflare zone the team controls and
   route a hostname (e.g. `openclaw-gw.<dev-zone>`) to `http://gateway:18789`.
2. Store the tunnel token: `CLOUDFLARED_TOKEN=... put-secrets.sh`.
3. Set `FrontDoorMode=tunnel` in `config/params.env` (the default). Bootstrap
   starts the overlay automatically and fails closed if its token is absent.
4. Leave `AllowedHttpsCidr` at its closed `127.0.0.1/32` default — `cloudflared`
   makes only **outbound** connections, so the security group needs **no** inbound
   rule at all. The stack permits only the required TCP/UDP `7844` tunnel
   transport plus HTTPS and DNS egress. Cloudflare terminates TLS at its edge.
   Add a **Cloudflare Access**
   service-token policy on the hostname for a second auth factor on top of the
   OpenClaw bearer token.

**Alternative — Route53 custom domain + Caddy (a stable named service you own end-to-end).**

1. Set `FrontDoorMode=caddy` in `config/params.env`. In a Route53 hosted zone
   the team owns, create `openclaw-gw.dev.<zone>` → the
   stack's Elastic IP (`PublicIp` output). Do not invent DNS ownership; if the
   team has no spare zone, register a cheap dedicated **dev** domain
   (~$1–12/yr). **Never** reuse the customer domain.
2. **Certificate issuance** — be deliberate about the ACME challenge:
   - *DNS-01 (keeps the SG restrictive, preferred):* use a Caddy image built with
     the `caddy-dns/route53` module and `tls { dns route53 }`. Grant the instance
     role a scoped `route53:ChangeResourceRecordSets` on that hosted zone only.
     No inbound is needed to issue the cert.
   - *HTTP/TLS-ALPN-01 (simplest, less tight):* Caddy validates over inbound 443,
     but Let's Encrypt validates from **many, changing** IPs — so a single-`/32`
     `AllowedHttpsCidr` will **fail issuance**. This path means opening 443 more
     broadly (auth still enforced by the bearer token).
3. **Reaching the gateway:** Laura runs on App Runner with **dynamic egress IPs**,
   so you cannot pin `AllowedHttpsCidr` to Laura. Either open 443 broadly
   (bearer-protected) or, better, keep this box closed and front it with the
   tunnel above. This egress reality is the main reason the tunnel is the
   recommended POC default.

**Do not** use a self-signed / Caddy `tls internal` cert.

### 5) Smoke test

```bash
OPENCLAW_GATEWAY_TOKEN='<token>' \
  infra/openclaw-gateway/scripts/smoke.sh https://openclaw-gw.dev.example.net
```

Asserts: `GET /healthz` and `/readyz` are healthy; `POST /v1/responses` **without**
a bearer is `401/403`; `POST /v1/responses` **with** the bearer is not rejected.
The token is never printed.

---

## Migrating off the Mac + trycloudflare gateway

The current setup runs OpenClaw on a Mac exposed through an **ephemeral**
`trycloudflare` URL that rotates on every restart, with no durable auth boundary.
Migration:

1. **Stand up** the AWS gateway (steps 0–5). Confirm `smoke.sh` is green on the
   new stable HTTPS URL.
2. **Repoint Laura** — change one variable value, `OPENCLAW_GATEWAY_URL`, on
   `laura-backend-next` from the `*.trycloudflare.com` URL to the new
   `https://openclaw-gw.dev.<zone>` (see the env-merge procedure below). The
   token/agent-id variables are unchanged in *name*; update the token *value* if
   you rotated it.
3. **Verify** a real dev-org meeting → chat reply flows through the new gateway
   (`OPENCLAW_EXPERIMENT_ORGS` = the dev org id).
4. **Retire the Mac**: stop the local gateway and the `trycloudflare` process.
   Because the token changed, the old URL is dead even if the tunnel lingers.
5. **Rotate** the old `OPENCLAW_GATEWAY_TOKEN` if it was ever shared, since the
   ephemeral tunnel offered no real protection.

---

## Laura backend variables — safe describe → merge → update (laura-backend-next ONLY)

Laura reads these variable **names** (`backend/app/core/config.py`,
`backend/app/openclaw/*`). Only these NAMES change; **no values live in git.**

| Variable NAME | Meaning | Value for the migration |
|---|---|---|
| `OPENCLAW_EXPERIMENT_ENABLED` | master on/off | `false` until you deliberately enable |
| `OPENCLAW_EXPERIMENT_ORGS` | allowlisted org ids (`*` = all) | the **dev** org id only |
| `OPENCLAW_AUTO_RUN` | auto-start runs | leave `false` |
| `OPENCLAW_GATEWAY_URL` | gateway base URL | `https://openclaw-gw.dev.<zone>` |
| `OPENCLAW_GATEWAY_TOKEN` | bearer token | the SecureString value (prefer an App Runner **RuntimeEnvironmentSecret**) |
| `OPENCLAW_AGENT_ID` | agent id header | `laura-executor-test` (default) |
| `OPENCLAW_BROWSER_ENABLED` | browser fallback tool | `true`/`false` per taste |

**An App Runner `update-service` replaces the *entire* env map.** Never send a
partial map — describe, merge, then update:

```bash
# DRY RUN — prints the merged config + the exact command, runs nothing:
AWS_REGION=eu-central-1 \
  infra/openclaw-gateway/scripts/laura-backend-env-merge.sh laura-backend-next
# then, after reviewing the diff, and only with the preconditions below:
AWS_REGION=eu-central-1 \
  infra/openclaw-gateway/scripts/laura-backend-env-merge.sh laura-backend-next --apply
```

The script **guards the service name** (`laura-backend-next` only — it refuses the
customer `laura-backend` / anything `frozen`), reads the current
`RuntimeEnvironmentVariables`, **adds only the missing OpenClaw names** (never
clobbering an existing value), and prints the merged `--source-configuration`
before applying.

**Preconditions before `--apply` (both are on you to confirm):**

- **No live meeting:** `GET /health` on `laura-backend-next` shows
  `active_sessions: 0` (deploying over a live meeting drops it).
- **No concurrent deploy:** `aws apprunner list-operations --service-arn <arn>`
  shows nothing in progress (App Runner serializes deploys and errors on a
  concurrent op).

**Never** run this against `laura-backend` (frozen/v1, customers) — the guard
blocks it, and so should you.

---

## Rollback to a disabled experiment

The experiment is inert whenever any of these is true, so rollback is a
one-variable change on `laura-backend-next` (via the same describe→merge→update
path) — **no gateway teardown required to make Laura safe:**

- `OPENCLAW_EXPERIMENT_ENABLED=false` (master off — the code default), **or**
- remove the dev org from `OPENCLAW_EXPERIMENT_ORGS`, **or**
- clear `OPENCLAW_GATEWAY_URL` (chat answers "OpenClaw gateway is not configured").

With the experiment off, Laura's legacy native/Pipedream executors resume and the
gateway is simply unused. **Experiment-off behaviour is preserved by
construction: this task changes no backend code.** After disabling, optionally
[tear down](#teardown) the stack to stop the ~$/mo cost.

---

## Backup / restore

- **Backup:** `scripts/backup.sh laura-openclaw-gw-dev` snapshots the data
  volume and prints the snapshot id. The stack's `DeletionPolicy: Snapshot` also
  snapshots on teardown, so the auth store/workspace is never silently lost.
- **Restore:** teardown → set `DataVolumeSnapshotId=snap-...` in `params.env` →
  `deploy.sh` boots a fresh instance from the snapshot (`scripts/restore.sh`
  prints these steps).

## Updating the gateway version

1. Resolve the new digest (step 0) and update `OpenClawImage` in `params.env`.
2. `deploy.sh laura-openclaw-gw-dev` — CloudFormation replaces the instance with
   the new pinned image; the data volume (auth/workspace) re-attaches. Snapshot
   first (`backup.sh`) if you want a rollback point.
3. `smoke.sh` to confirm.

## Teardown

```bash
# 1) FIRST disable the experiment on laura-backend-next (rollback section)
# 2) then:
infra/openclaw-gateway/scripts/teardown.sh laura-openclaw-gw-dev
```

Guarded on the stack name, requires typing the name to confirm, snapshots the
data volume on delete, and reminds you to remove the leftover snapshot and the
`/laura/dev/openclaw/*` SSM secrets when you are done.

---

## Cost estimate (eu-central-1, assumptions stated)

Always-on, single `t4g.small`, 20 GB gp3, low dev traffic:

| Item | Assumption | ~ Monthly |
|---|---|---|
| EC2 `t4g.small` | on-demand, 24×7 | $12–13 |
| EBS gp3 20 GB | baseline IOPS/throughput | $2 |
| Elastic IP | attached to a running instance | $0 (idle EIP ≈ $3.6) |
| CloudWatch logs | a few GB ingest+store, 30-day | $1–3 |
| Snapshots | ~20 GB incremental | ~$1 |
| Route53 zone | only if a **new** dev zone | $0.50 |
| **Total (always-on)** | | **~$18–30 / month** |
| **Stopped between tests** | EBS + snapshots only | **~$5–8 / month** |

**Not included:** the gateway's **LLM usage** (`ANTHROPIC_API_KEY`) is
usage-based and billed separately by the model provider — size it from expected
test volume, not from this infra line. Cloudflare Tunnel + Access are free at
this scale. Fargate+ALB would add ≈$16–18/mo just for the load balancer, which is
part of why it was rejected for the POC.

---

## Security posture & residual risks

**Enforced by this IaC:** pinned image digest (never `latest`); TLS at the proxy
with HSTS; mandatory OpenClaw bearer token (auth **not** delegated to the proxy);
secrets only in SSM SecureString → root-only `0600` env file; least-privilege IAM
(SSM read scoped to `/laura/dev/openclaw/*`, KMS decrypt via-SSM only, logs scoped
to one group); security group **closed by default** (egress 443/DNS only, no SSH);
IMDSv2 required; SSM Session Manager instead of SSH; `cap_drop NET_RAW/NET_ADMIN`
+ `no-new-privileges`; gateway port `18789` on an `internal: true` network, never
host-published; CloudWatch logs with retention; instance auto-recovery; encrypted
EBS with snapshot-on-delete; a **dev-only guard** that fails closed on customer
resource names.

**Residual risks / operator responsibilities:**

- **The direct-443 (Caddy) path trades away a closed SG.** App Runner's dynamic
  egress and ACME's changing validator IPs mean you cannot pin `AllowedHttpsCidr`
  to a single `/32` and still work; opening 443 leaves the gateway internet-
  reachable (mitigated only by the bearer token). Prefer the **tunnel path**,
  which needs no inbound at all, or use Caddy **DNS-01** to at least avoid inbound
  for issuance. Keep `AllowedHttpsCidr` closed unless you have accepted this.
- **Compose plugin checksum** in `user-data.sh` ships as `REPLACE_WITH_SHA256`;
  fill it to enforce supply-chain verification of the Compose binary. Caddy is
  pinned by tag — append its `@sha256` for full pinning.
- **Single node, single AZ** — a POC has no HA; the auto-recovery alarm handles
  host failure, not AZ failure. Restore-from-snapshot is the recovery story.
- **One shared token** — rotate it on any suspected exposure (`put-secrets.sh`
  overwrite → re-attach on the instance and update `laura-backend-next`).
- **Trusting the proxy for client IPs** is enabled only for logging; auth is
  never delegated to the proxy (that OpenClaw "trusted-proxy-auth" feature is a
  known footgun and is deliberately **not** used).
- This is **dev, single-tenant** — see [Not multi-tenant](#not-multi-tenant).

---

## Validation (key-free, local)

```bash
python3 infra/openclaw-gateway/tests/validate_infra.py     # structural invariants
python3 infra/openclaw-gateway/tests/test_dev_guard.py     # guard accept/deny
# or, via pytest:
pytest infra/openclaw-gateway/tests/ -q
git diff --check                                           # whitespace hygiene
```

No AWS credentials, keys, or network needed. The validator enforces: pinned
digest, IMDSv2, no SSH/KeyName, closed ingress, scoped IAM, retention, encrypted
snapshotted volume, container hardening, and that **no secret- or account-id-
shaped strings** are committed anywhere under `infra/openclaw-gateway/`.
