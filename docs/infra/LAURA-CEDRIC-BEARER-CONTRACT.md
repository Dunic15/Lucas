# Laura ↔ Cedric: per-workspace bearer contract (Option A)

**Decision (2026-07-13):** resolve tenancy from a **per-workspace bearer token**,
not a shared global credential. This closes PR D item 3 and reconciles it with
item 5 (which today ships only `webhook_secret`, never `webhook_token`).

Chosen over Option B (per-org HMAC everywhere + shared deployment bearer) because:
- GET routes (`/context`) can't HMAC a body; a per-org **bearer** authenticates a
  GET naturally; no query-string HMAC canonicalization foot-gun.
- The token **is** the org → eliminates the "trust the claimed `org_id`" bug class
  entirely. For a self-serve tenant boundary, the design that can't be gotten
  wrong wins.
- Clean revoke = delete the token row (matches PR D "revoke on disconnect").
- Contract change is a single additive field; Laura already stores per-org
  `webhook_secret`, so storing `webhook_token` is symmetric.

---

## The contract change

**1. `/api/slack/complete` payload gains `webhook_token`.**
On zero-touch install, Cedric already mints a per-org `webhook_token` + `webhook_secret`
but only delivers the secret. Add the token to the server-to-server POST to
`{return_url}/dashboard/connections/brain/slack/complete`:

```json
{ "org_id", "avatar_id", "team_id", "channel", "webhook_secret", "webhook_token" }
```
The token still never enters a browser URL (server-to-server only, Bearer
`LAURA_API_TOKEN`), same as `webhook_secret` today.

---

## Laura side (owned by the PR D session: this doc is the spec, not an edit)

1. **Store `webhook_token` per org.** Mirror the existing per-org secret store
   (`LAURA_WEBHOOK_SECRETS_BY_ORG` → add `LAURA_WEBHOOK_TOKENS_BY_ORG`, or a
   `webhook_token` column on the control-plane org row once PR A's Postgres lands).
   Encrypted at rest, never in `config_json`, never logged.
2. **Send the per-org bearer on every Laura→Cedric call**, not the shared token:
   `/events` (webhook), `/context`, `/orgs`, `/actions/status`, connectors; all use
   `Authorization: Bearer <that org's webhook_token>`.
3. **Keep per-org HMAC on `/events`** (defense in depth: the token authenticates,
   the HMAC binds the payload).
4. **Update `complete_brain_slack_install` (`backend/app/dashboard.py`)** to accept +
   persist `webhook_token` from the payload.

## Cedric side (owned by Ben)

1. Add `webhook_token` to the `/slack/complete` payload (provisioning).
2. Build a **`hash(token) → org` resolver** (a reverse index; store `sha256(token)`,
   never the token).
3. Resolve org from the token on every inbound route; **stop reading `org_id` from
   the body/query** as the identity source.
4. **Drop the shared-bearer fallback route-by-route** once Laura is sending per-org
   tokens (see rollout).
5. Revoke = delete the token row on disconnect (+ provider-side token revoke).

## Rollout (no big-bang)

1. Cedric adds `webhook_token` to `/slack/complete` and the token→org resolver, but
   **keeps accepting the shared bearer** (dual-accept).
2. Laura ships storage + per-org bearer on all calls; re-provision existing orgs so
   each gets its `webhook_token`.
3. Verify every live org authenticates via its per-org token (logs/metrics).
4. Cedric **drops the shared-bearer fallback** route-by-route; `/context` (the
   weakest spot today) goes first.
5. Two-workspace isolation test on real infra: workspace A's token cannot read or act
   on workspace B's org, on **every** route.

## Definition of done
- No inbound Cedric route trusts `org_id` from the body/query for identity.
- `/context` (GET) authenticates via the per-org bearer; shared-token fallback gone.
- Per-org HMAC still enforced on `/events`.
- Revoke on disconnect kills the token both in Cedric's index and at the provider.
- Two-workspace isolation test green on real infra.
