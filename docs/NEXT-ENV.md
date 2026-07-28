# Two environments: frozen for customers, `next` for you

## The two links

**Customers — send them this, it is unchanged:**
```
https://app.lauravatar.com
```

**You — never share this one:**
```
https://48zmdue8kg.eu-central-1.awsapprunner.com/dashboard
```

> ⚠️ **Include `/dashboard`.** The site root `/` is the old *Demo Console* (ask
> an avatar, paste a transcript) — it is served identically by BOTH
> environments, you just never land on it via the customer link. Opening the
> bare host and seeing the Demo Console is the single most convincing way to
> conclude "this is a different site". It is the same site, one path up.

> ⚠️ The address ends in **`.com`**, not `.co`. Dropping the final `m` gives
> `DNS_PROBE_FINISHED_NXDOMAIN` and looks exactly like the service being down —
> it isn't. Copy-paste it, don't retype it. (If you want to stop worrying about
> this, attach a custom domain such as `next.lauravatar.com`; see the end of
> this file.)

**Before the `next` link will let you in**, its two OAuth redirect URIs must be
authorised in Google Cloud Console → Credentials → the Laura OAuth client →
*Authorized redirect URIs*:
```
https://48zmdue8kg.eu-central-1.awsapprunner.com/auth/google/callback
https://48zmdue8kg.eu-central-1.awsapprunner.com/oauth/google/callback
```
The first is sign-in (`{PUBLIC_BASE_URL}/auth/google/callback`, `core/auth.py:73`);
the second is connecting Calendar/Gmail (`GOOGLE_CALENDAR_REDIRECT_URI`). Until
they are added, login fails with `redirect_uri_mismatch`.

**Is it just paused?** `next` is billed even when idle, so it may have been
paused deliberately:
```bash
aws apprunner describe-service --region eu-central-1 --service-arn <next-arn> \
  --query 'Service.Status'      # RUNNING | PAUSED
aws apprunner resume-service   --region eu-central-1 --service-arn <next-arn>
```

Set up 2026-07-28, the day v1 was frozen. The freeze alone protects customers
but also blocks *you* — with one service pinned to `frozen/v1`, a push to `main`
reaches nobody, including the person who wrote it. This is the second half.

```
app.lauravatar.com   →  laura-backend       →  frozen/v1  →  CUSTOMERS
48zmdue8kg…          →  laura-backend-next  →  main       →  YOU
```

Push to `main` → it deploys to **your** service only. Customers keep v1 until
you deliberately flip them (`docs/FREEZE-V1.md`).

## The two services

| | customers | you |
|---|---|---|
| service | `laura-backend` | `laura-backend-next` |
| branch | `frozen/v1` | `main` |
| URL | `app.lauravatar.com` | `48zmdue8kg.eu-central-1.awsapprunner.com` |
| ARN suffix | `f169c4a4…` | `b349e52c…` |

Everything else — IAM role, autoscaling config, health check, 32 SSM secrets —
is identical, cloned from the live service so the two behave the same.

## What is deliberately different

Five variables. Each one exists to stop the two environments touching:

| variable | customers | you | why |
|---|---|---|---|
| `PUBLIC_BASE_URL` | `app.lauravatar.com` | the `next` URL | **Recall's webhook is per-bot**, built from this at bot creation (`recall_client.py:281`). Each service therefore gets its own transcripts back. This is what makes two environments possible at all. |
| `EL_AGENT_NAME_SUFFIX` | *(empty)* | `" (v2)"` | `create_meeting_agent.py` is idempotent by agent NAME; without this it rewrites the customers' agents |
| `VOICE_AGENT_RELAY_WS_BASE` | `cedric-voice` | `cedric-voice-v2` | a `wrangler deploy` must not swap the bridge under a live meeting |
| `LITESTREAM_REPLICA_URL` | `…/store` | `…/store-v2` | two writers on one prefix corrupts both |
| `GMAIL_WATCH_ENABLED` | `true` | **`false`** | otherwise BOTH services see the same email invite and TWO avatars join the same call |

That last one is the trap that bites in a meeting, not in a log.

## The agents

Separate, verified disjoint on 2026-07-28:

| | customers (`frozen/v1`) | you (`main`) |
|---|---|---|
| Laura | `agent_9601kyg7…` | `agent_8301kymj4a6efq9r9hsdjzr29mg9` |
| Cedric | `agent_0801ky9q…` | `agent_0901kymj4gv1fjfs6pn71b80x0by` |

The ids live in `avatars/*/avatar.yaml`, which is why they differ per branch.
After creating the v2 pair the customers' agents were re-read: tools 11, KB
20/12, prompt bytes unchanged. **Never point both branches at one id** — tools,
knowledge, LLM, voice and turn settings are server-side on the agent and are
*not* overridable per connection.

## The data: seeded once, then it diverges

`next` started with an EMPTY database, and that made it look like a different
product — same HTML byte for byte, but no meetings, no avatars configured, no
connections. So on 2026-07-28 the replica was **seeded from a copy of the
customers' one**: 212 objects, 9.8 MB, both environments then reporting the same
20 meetings.

How it was done (repeat this whenever `next` has drifted too far and you want a
fresh copy of reality):

```bash
aws apprunner pause-service --region eu-central-1 --service-arn <next-arn>
# wait for PAUSED — otherwise litestream is still writing while you copy
aws s3 rm   s3://laura-org-memory/store-v2/ --recursive
aws s3 sync s3://laura-org-memory/store/ s3://laura-org-memory/store-v2/
aws apprunner resume-service --region eu-central-1 --service-arn <next-arn>
```

The boot wrapper (`scripts/start-with-litestream.sh`) restores from the replica,
so `next` comes back up holding that snapshot.

Three things to know:

- **They diverge from that moment.** Anything you do in `next` never reaches the
  customers, and anything the customers do never reaches `next`. Re-seed when
  the gap starts to matter.
- **`store/` is read-only in this procedure.** The sync only ever writes to
  `store-v2/`. Never invert the arguments — `store-corrupt-20260722/` in that
  bucket is what a bad day here looks like.
- **It is real data**, including org credentials (the Google token encryption key
  is the same secret in both services, so connections carry over and work). Same
  AWS account, same company — but treat `next` with the same care as production,
  because the contents are production's.

## How to use it

**Iterate:** push to `main` → `laura-backend-next` redeploys itself (~7 min) →
test.

**Dispatch a test avatar:** from the `next` dashboard, or
`POST /sessions/start` against the `next` URL. The email invite path is off
there on purpose — invite it explicitly, so you always know which environment a
bot came from.

**Change an agent's tools/knowledge/voice:**
```bash
EL_AGENT_NAME_SUFFIX=" (v2)" ELEVENLABS_API_KEY=… \
  python backend/scripts/create_meeting_agent.py --avatar petra
```
**The suffix is not optional.** Without it the script silently rewrites the
agent your customers are talking to, and rolling the backend back will not undo
it, because the damage is not in git. Run `--dry-run` first and read the `name`.

**Change turn-taking:** edit `relay/cedric-voice-v2/`, deploy from that folder.
`relay/cedric-voice/` belongs to the customers — leave it alone.

## Cost

A second App Runner service bills whether or not you use it. When you are not
testing:

```bash
aws apprunner pause-service  --region eu-central-1 --service-arn <next-arn>
aws apprunner resume-service --region eu-central-1 --service-arn <next-arn>
```

Pausing stops the compute charge; the service, its URL and its config survive.

## Ship it to customers

When `main` is ready, repoint the CUSTOMER service from `frozen/v1` to `main` —
recipe in `docs/FREEZE-V1.md`. At that moment the customers' avatars start using
the v2 agents (the ids are on `main`), which is correct: `main` is then the
product. Cut a fresh `frozen/*` tag first, so the version you are leaving stays
reachable.

## Optional: a nicer URL

`48zmdue8kg…` is machine-generated and changes if the service is ever recreated
— which would also invalidate the Google redirect URIs you authorised. Attaching
a custom domain fixes both:

```bash
aws apprunner associate-custom-domain --region eu-central-1 \
  --service-arn <next-arn> --domain-name next.lauravatar.com
```

It returns the DNS records to add. `lauravatar.com` is on the
`Duccioprofeti@gmail.com` Cloudflare account — the same one that owns the
`cedric-voice` workers. Then authorise `https://next.lauravatar.com/...` in
Google once, and the URL never changes again.
