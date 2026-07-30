# OpenClaw gateway — development only

The OpenClaw runtime Laura's backend calls to execute already-approved
actions. **This package configures exactly one service: `laura-backend-next`.**
It refuses, mechanically, to touch customer infrastructure.

> ⚠️ `laura-backend` (no suffix) is the **customers'** App Runner service and
> must never be configured by anything here. `frozen/v1`, `relay/cedric-voice`
> and `create_meeting_agent.py` are likewise customer-owned. `guards.py`
> rejects all of them and has no override flag.

## What Laura actually calls

```
POST https://<GATEWAY_DOMAIN>/v1/responses
Authorization: Bearer <OPENCLAW_GATEWAY_TOKEN>
x-openclaw-session-key: laura-openclaw-<run_id>
x-openclaw-agent-id: laura-executor-test
```

The backend sends an OpenAI-Responses-shaped request carrying client function
tools; the gateway replies with tool calls, the **backend** executes them
against its own approval/exactly-once ledger, and returns results. The gateway
never performs a vendor write itself — that boundary is what keeps the
approval door meaningful.

## Architecture

```
        Internet
           │  :443 (only public listener)
    ┌──────▼───────────────────────────────┐
    │ EC2 t4g.small (arm64), eu-central-1  │
    │                                      │
    │  caddy ──TLS/ACME──► openclaw:8080   │  ← expose only, no host port
    │    │                    │            │
    │  certs (vol)         state (vol)     │
    └────┴────────────────────┴────────────┘
              EBS gp3 20 GB (docker data-root)
                     ▲
              SSM SecureString  (secrets, fetched at boot)
```

- **Primary model is explicit**: `anthropic/claude-opus-5`, pinned in
  `openclaw.json` for both `agents.defaults` and the `laura-executor-test`
  agent, so a new image cannot silently change which model executes actions.
- **State persists** on an EBS-backed Docker volume across restarts and image
  bumps.
- **No laptop dependency**: a systemd unit plus `restart: unless-stopped`
  brings the gateway back after reboot with nothing else running.
- **Pinned by digest** — no floating `latest` anywhere.

## Secrets

Never in git, never in the image. `bin/bootstrap.sh` reads them at boot from
SSM Parameter Store (SecureString, KMS at rest) into `/opt/openclaw/secrets.env`
(root-owned, `0600`), which compose loads via `env_file`:

```
/laura/next/openclaw/ANTHROPIC_API_KEY
/laura/next/openclaw/OPENCLAW_GATEWAY_TOKEN
```

A missing parameter **fails the bootstrap** rather than starting a gateway
with an empty token. Backups deliberately exclude secrets, so a stolen archive
cannot leak a key; `bin/restore.sh` relies on SSM to re-supply them.

## Authentication and TLS

Authentication is mandatory at two layers: OpenClaw's own
`server.auth.required = true`, and Caddy rejecting any request without a
bearer token before it reaches the runtime. Only `/v1/responses` and `/health`
are proxied; everything else returns 404, so no admin or debug surface is
exposed. TLS is Let's Encrypt via ACME HTTP-01, renewed automatically, with
HSTS set.

## Deploy

Requires AWS credentials for the **development** account. Nothing below has
been run — no AWS resource exists yet.

```bash
# 1. secrets (once)
aws ssm put-parameter --region eu-central-1 --type SecureString \
  --name /laura/next/openclaw/ANTHROPIC_API_KEY      --value "$ANTHROPIC_API_KEY"
aws ssm put-parameter --region eu-central-1 --type SecureString \
  --name /laura/next/openclaw/OPENCLAW_GATEWAY_TOKEN --value "$(openssl rand -hex 32)"

# 2. host: t4g.small + 20GB gp3 data volume + Elastic IP, instance role with
#    ssm:GetParameter on /laura/next/openclaw/* and kms:Decrypt only.
#    Security group: 80/443 from 0.0.0.0/0, 22 from your IP only.

# 3. on the instance
sudo install -d /opt/openclaw && sudo cp -r gateway/* /opt/openclaw/
echo "GATEWAY_DOMAIN=openclaw-next.example.com" | sudo tee /opt/openclaw/gateway.env
sudo GATEWAY_DOMAIN=openclaw-next.example.com bash /opt/openclaw/bin/bootstrap.sh

# 4. verify
GATEWAY_DOMAIN=openclaw-next.example.com bash bin/health.sh
```

### Point the development backend at it

**Only `laura-backend-next`.** Set these three variables on that service and
no other:

```
OPENCLAW_GATEWAY_URL=https://openclaw-next.example.com
OPENCLAW_GATEWAY_TOKEN=<the SSM value>
OPENCLAW_EXPERIMENT_ENABLED=true
OPENCLAW_EXPERIMENT_ORGS=<one org uuid>      # start with a single org
```

Leave `OPENCLAW_AUTO_RUN` at its default. Rolling back is setting
`OPENCLAW_EXPERIMENT_ENABLED=false` — the surface goes inert immediately and
no gateway change is needed.

## Rollback and teardown

```bash
bash bin/backup.sh                       # tarball of state + certs
bash bin/restore.sh <archive.tar.gz>     # restore onto a fresh host
bash bin/teardown.sh                     # guarded; backs up, then removes
```

`teardown.sh` deliberately does **not** delete the EC2 instance, EBS volume,
Elastic IP or SSM parameters — it prints the commands instead, so destroying
paid resources stays a deliberate act.

## Cost (idle, eu-central-1, approximate)

| Resource | Monthly |
|---|---|
| EC2 `t4g.small` on-demand | ~$12 |
| EBS gp3 20 GB | ~$1.60 |
| Elastic IP (attached) | $0 |
| SSM Parameter Store (standard) | $0 |
| **Total idle** | **~$14/mo** |

Anthropic usage is billed separately and only when a run executes.

## Limitations (read before relying on this)

- **Never deployed.** Every command here is unrun; the image digests are
  placeholders that must be replaced with real ones before first boot
  (`docker buildx imagetools inspect ghcr.io/openclaw/openclaw:<version>`).
- **Single instance, no HA.** A stopped instance is an outage. Acceptable for
  development; not a production topology.
- **`/health` is liveness, not correctness** — it does not prove the
  Anthropic key is valid. `bin/health.sh` with a token exercises the real path.
- **A domain is required.** ACME cannot issue for a raw IP; without a DNS
  record TLS will not complete.
- **The gateway can reach the internet.** Egress is not restricted, so a
  compromised runtime could call out. Adding a NAT/egress allowlist is the
  obvious hardening step and is not done here.
