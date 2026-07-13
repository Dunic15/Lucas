# Cedric workspace credential runbook

Laura and Cedric use three deliberately separate credential classes.

| Direction | Credential | Storage |
|---|---|---|
| OAuth bootstrap, both services | `CEDRIC_ORGS_TOKEN` | SecureString on each service; never a browser value |
| Laura → Cedric | Per-org `webhook_token` plus HMAC `webhook_secret` | Laura SSM registry paths below |
| Cedric → Laura | Per-org `org_token` | Cedric secret store; Laura stores SHA-256(raw) in `org_tokens` only |

`LAURA_API_TOKEN`, `LAURA_WEBHOOK_TOKEN`, and `LAURA_CONTEXT_TOKEN`
remain Demo-only. They are not customer tenant credentials.

## Slack handoff

Laura sends the browser to Cedric's stable
`/api/slack/install?state=...` endpoint. The signed state includes
`org_id`, `avatar_id`, `channel`, `return_url`, `complete_url`,
`exp`, and `nonce`.

Cedric must POST the signed state opaquely to `complete_url` with:

```json
{
  "org_id": "...",
  "avatar_id": "cedric",
  "team_id": "...",
  "channel": "...",
  "webhook_secret": "...",
  "webhook_token": "...",
  "state": "..."
}
```

and `Authorization: Bearer <CEDRIC_ORGS_TOKEN>`.

The completion response contains `org_token`. A retry of the same state
returns the same token; a newer install nonce rotates it. The token is never
placed in `state`, a redirect, a browser query, SSM, or logs.

Use one exact Cedric origin for every URL; authenticated requests do not follow
an apex-to-`www` redirect:

```text
PUBLIC_BASE_URL=https://app.lauravatar.com
CEDRIC_ORGS_URL=https://www.meet-cedric.com/api/laura/orgs
SURFACE_WEBHOOK_URL=https://www.meet-cedric.com/api/laura/events
SURFACE_CONTEXT_URL=https://www.meet-cedric.com/api/laura/context
```

## App Runner SSM permissions

The App Runner instance role needs the following actions for the per-org
credentials it hot-writes during connect/disconnect:

- `ssm:GetParameter`
- `ssm:GetParametersByPath`
- `ssm:PutParameter`
- `ssm:DeleteParameter`
- `kms:Encrypt` and `kms:Decrypt` only when these SecureStrings use a
  customer-managed KMS key

Scope SSM resources to:

```text
arn:aws:ssm:eu-central-1:<account-id>:parameter/laura/prod/LAURA_WEBHOOK_SECRETS_BY_ORG
arn:aws:ssm:eu-central-1:<account-id>:parameter/laura/prod/LAURA_WEBHOOK_SECRETS_BY_ORG/orgs/*
arn:aws:ssm:eu-central-1:<account-id>:parameter/laura/prod/LAURA_WEBHOOK_SECRETS_BY_ORG/bearers/*
```

The aggregate parameter is compatibility-only; dedicated `orgs/*` HMAC
secrets and `bearers/*` peer tokens are authoritative. Do not grant the
runtime role access to `LAURA_DATABASE_ADMIN_URL`.

## Release proof

Before customer traffic, run two real Slack workspaces through connect,
callback/context/connectors, action status, reinstall, and disconnect. Prove
that each bearer resolves its org server-side, body/query `org_id` cannot
change scope, stale state cannot rotate credentials, same-state completion is
idempotent, and disconnecting workspace A leaves workspace B untouched.
