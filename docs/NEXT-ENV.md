# Two environments: frozen for customers, `next` for you

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
