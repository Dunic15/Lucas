# infra/openclaw-gateway

Infrastructure-as-code for an **always-on, single-tenant development** OpenClaw
gateway on AWS — the secure replacement for the Mac + ephemeral `trycloudflare`
setup. Scoped to **one Laura development test org**; this is **not** a hostile
multi-tenant deployment.

**Read the runbook first:** [`docs/OPENCLAW-GATEWAY-AWS.md`](../../docs/OPENCLAW-GATEWAY-AWS.md)
— architecture decision (and why not App Runner / Fargate), prerequisites,
migration, the safe `laura-backend-next` env merge, smoke tests, rollback,
cost, and teardown.

## Architecture, in one line

One ARM EC2 instance → Docker Compose: pinned `ghcr.io/openclaw/openclaw@sha256`
on an internal-only network, behind a Caddy TLS reverse proxy → persistent
encrypted EBS. Secrets in SSM SecureString, least-privilege IAM, closed-by-
default security group, IMDSv2, SSM-only admin, CloudWatch logs with retention,
auto-recovery, snapshot-on-delete.

## Guardrails baked in

- **Dev-only guard** (`scripts/dev_guard.py`): every mutating script refuses
  customer/`frozen` resource names and fails closed on unknown ones.
- **Pinned image only**: the template `AllowedPattern` and `deploy.sh` reject
  anything that is not `...@sha256:<digest>` — no `:latest`.
- **No secrets / account ids in git**: enforced by `tests/validate_infra.py`.

## Quickstart

```bash
# validate locally (no AWS, no keys)
python3 tests/validate_infra.py && python3 tests/test_dev_guard.py

# deploy to a DEV account (human-run; see the runbook for the full flow)
cp config/params.example.env config/params.env      # fill in, add pinned digest
export SECRETS_PATH_PREFIX=/laura/dev/openclaw AWS_REGION=eu-central-1
OPENCLAW_GATEWAY_TOKEN=... ANTHROPIC_API_KEY=... scripts/put-secrets.sh
scripts/deploy.sh laura-openclaw-gw-dev
OPENCLAW_GATEWAY_TOKEN=... scripts/smoke.sh https://openclaw-gw.dev.example.net
```

Nothing here runs AWS commands on its own; the scripts are for an operator with
dev-account access.
