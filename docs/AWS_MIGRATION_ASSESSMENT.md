# Laura → AWS Migration Assessment

_Assessment date: 2026-07-02. Author: Claude (Opus 4.8), from the actual codebase._

## TL;DR

- **Serverless (Lambda + API Gateway + DynamoDB):** a **2–4 week rewrite**, not a
  migration. It fights Laura's architecture and makes latency/glitches **worse**
  (cold starts). **Not recommended.**
- **Container (App Runner / Lightsail / ECS Fargate), existing Docker image:** a
  **1–2 day lift-and-shift**, no code changes. The only sane AWS path.
- **Region matters:** must be **eu-central-1 (Frankfurt)** to sit next to Recall.
  The Lambda console link that started this was `us-east-1` — wrong side of the
  Atlantic.
- **Nothing on AWS fixes the 2–4s first-token** (that's Anthropic-side) or is
  cheaper-with-effort than the **one-click Render Standard (2 GB) upgrade** that
  fixes the 512 MB glitch problem today.

---

## What Laura actually is (why architecture choice matters)

Laura is a **stateful, always-on service**, not a request→response API. Four
properties in the code decide everything:

| Property | Where | Implication |
|---|---|---|
| **Persistent WebSocket push** | `avatar_ws` / `_make_avatar_speak` (main.py) | The backend holds an open WS to the avatar page and *pushes* streamed speak-chunks into it. |
| **Always-on background loop** | `_gmail_watch_loop` (polls every 15s) | A long-lived asyncio task that never returns. |
| **In-memory session state** | `store.py` (sessions, transcripts, WS handles) | Live state kept in the process, not a DB. |
| **In-process RAG model** | `fastembed` / onnxruntime, warmed at boot | ~200–300 MB model loaded once and reused. |

The Dockerfile already runs this as a single `uvicorn` container and explicitly
targets "any container host (Render, Fly, Railway, Cloud Run, AWS VM)."

---

## Option A — Serverless (Lambda + API Gateway + DynamoDB): NOT recommended

Lambda is stateless and ephemeral (dies after each request, 15-min max). Every one
of Laura's four properties breaks:

| Laura needs | Lambda reality | Rewrite required |
|---|---|---|
| Hold an open WebSocket, push chunks | Lambda can't hold a connection | Rebuild on **API Gateway WebSocket API** + **DynamoDB** connection store; rewrite the entire streaming-to-Anam path |
| Poll Gmail every 15s forever | Lambda can't run a loop | Rebuild as **EventBridge** scheduled invocations |
| In-memory sessions/transcripts | Cold, stateless per invocation | Externalize **all** state to DynamoDB/Redis |
| fastembed model resident | Reloads on cold start (seconds); 250 MB package limit | Provisioned concurrency (= always-on billing) or a container-image Lambda |

**The irony:** to avoid Lambda cold-start latency on the first-token path (the
exact thing we're trying to speed up) you buy **provisioned concurrency**, which is
always-on billing — so the "pay per request" saving disappears and you're paying
for a running server anyway, just a more complex one.

- **Effort:** ~**2–4 weeks** rewrite + full re-test of the Recall / Anam / Gmail /
  calendar / OAuth flows (every bug already fixed is back on the table).
- **Outcome:** worse latency (cold starts), more moving parts, more glitch surface.
- **Verdict:** ❌ Wrong tool. Do not do this.

---

## Option B — Container on AWS (the sane path, if AWS at all)

Run the **existing Docker image unchanged** on a managed container service. No code
rewrite; WebSockets, background loop, and in-memory state all keep working.

**Service choice (Frankfurt, `eu-central-1`):**

| Service | Model | Notes |
|---|---|---|
| **App Runner** | Managed, closest to Render | Auto HTTPS, auto-restart, deploy from image/repo. WebSocket support is limited/newer — verify before committing. |
| **Lightsail Containers** | Flat-rate, simplest | 1 GB "Small" ~$20/mo, 2 GB "Medium" ~$40/mo. Managed HTTPS + restart. Easiest lift-and-shift. |
| **ECS Fargate + ALB** | Most control | ALB handles WebSockets well; more setup (task def, ALB, ACM cert, target group). |

**Migration checklist (≈ 1–2 days):**

1. Push the Docker image to **ECR** (or point the service at the GitHub repo).
2. Create the service in **eu-central-1** with 2 GB RAM / 1 vCPU.
3. Re-enter **every env var / secret** (Anthropic, Recall, Anam, Google OAuth,
   SendGrid, `PUBLIC_BASE_URL`).
4. Attach **persistent storage** for the SQLite DB (or move to RDS Postgres) so
   sessions survive restarts.
5. Get the new HTTPS URL → set `PUBLIC_BASE_URL`.
6. **Re-point every webhook/redirect** (this is where things silently break):
   - Recall realtime transcript endpoint (`PUBLIC_BASE_URL/webhooks/recall`)
   - Recall calendar webhook (`/webhooks/recall-calendar`)
   - Google OAuth redirect URI (`/oauth/google/callback`) — must match Google
     Cloud console exactly or calendar auth 401s
7. Point the domain (`lauravatar.com` Worker) at the new backend.
8. Re-run the full live test: Add-people join → she hears → she answers → female
   voice, one bot, no duplicate.

- **Effort:** ~**1–2 days** + a careful re-test pass.
- **Outcome:** same behavior as today; **does not** speed up first-token.
- **Verdict:** ✅ Possible and safe *if* you specifically want to be on AWS.

---

## Cost with the $100 Activate credit

- Render Standard (2 GB): ~$25/mo out of pocket.
- AWS container (2 GB, Frankfurt): ~$25–45/mo → **$100 credit lasts ~2–4 months**,
  then out of pocket. Credit **expires Jan 2027** and can auto-convert to a paid
  plan if certain services are touched.

The credit delays ~$25/mo for a couple of months; it does not make hosting free
long-term.

---

## What migrating does NOT fix

- **The 2–4s time-to-first-token.** That is Anthropic API latency (Haiku is already
  the fastest model; no faster tier exists). Identical from Render, EC2, App Runner,
  or Lambda. Fix path is prompt-cache + smaller prompt (already deployed) and, if it
  persists, an **Anthropic account rate-limit tier** check in the Console.
- **The 512 MB glitch/OOM problem.** Fixed by *more RAM* — one click on Render
  Standard, or a right-sized container anywhere. Not an AWS-specific benefit.

---

## Recommendation

1. **Today:** Render → Settings → Instance Type → **Standard (2 GB)**. Five minutes,
   removes OOM/restart glitches, keeps `local` retrieval quality, zero migration
   risk.
2. **Measure** the `[latency] llm usage` line from one live call to settle whether
   the 2–4s is prompt size (code) or Anthropic tier (Console).
3. **AWS later, deliberately, if you want it** — as the **container** option in
   **eu-central-1**, not serverless. Spend the $100 credit on a **staging copy** or
   experiments, not on betting the live avatar on a rushed migration.
