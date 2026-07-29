# OpenClaw demo runbook

How to run and show the connected-app demo on `claude/openclaw-demo-ready`:
one meeting ask becomes an approvable Notion page, and a chat bound to that
meeting answers about it and proposes a follow-up without running it.

Everything below was verified on this branch. Where something does **not**
work, it says so — read [Known live limitations](#known-live-limitations)
before you demo, not after.

---

## The one thing that will ruin the demo if you skip it

**The Action Center demo and the Chat demo need different flags**, unless you
have an OpenClaw gateway.

`OPENCLAW_EXPERIMENT_ORGS` is what turns Chat on for a workspace. It *also*
re-routes that workspace's meeting actions away from the direct connected-app
executor and onto the OpenClaw runner. The runner only owns actions belonging
to a run created at meeting-finalize time, so approving anything else answers
**HTTP 409** and settles the action **failed** — with zero vendor writes.

That behaviour is correct (it refuses to execute what it cannot account for),
but it means:

| Demo | `OPENCLAW_EXPERIMENT_*` | Needs a gateway |
|---|---|---|
| **A — Action Center**: capture → approve → verified Notion page | **off** | no |
| **B — Chat**: meeting-bound chat answers + proposes | **on** | yes, for replies |
| Both, one workspace | on | yes, plus real finalized meetings |

Pinned by `test_the_two_demo_configurations_are_mutually_exclusive_without_a_gateway`.

---

## Local startup

Python 3.11+. From the repository root:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn backend.app.main:app --port 8000
```

Open <http://127.0.0.1:8000/dashboard>. With `.env` untouched the offline demo
runs on the stub brain and hash embeddings — no keys, ever.

For a throwaway store (recommended when demoing, so you can reset by deleting
one file):

```bash
LAURA_STORE_PATH=/tmp/laura-demo.sqlite3 \
BRAIN_PROVIDER=stub EMBEDDING_PROVIDER=hash \
uvicorn backend.app.main:app --port 8000
```

### Seeding demo A without a live meeting

The Action Center reads finalized meeting artifacts. To show it without
sending an avatar into a real call, run the finalize typing pass directly:

```bash
LAURA_STORE_PATH=/tmp/laura-demo.sqlite3 python3 - <<'PY'
import sys; sys.path.insert(0, "backend")
from app import store
from app.brain import engine
from app.config import settings

org = settings.demo_org_id
brief = ("The team walked through the OpenClaw connected-app plane and agreed "
         "the next three steps before the pilot.")
ask = ("Create a Notion page called OpenClaw Demo, add a short meeting "
       "summary, and add a checklist with the next three steps.")
actions = engine.type_actions(
    [{"action_id": "act_openclaw_demo", "item": ask, "owner": "Cedric"}],
    brief, provider="stub")
store.save_artifact("bot_openclaw_demo", {
    "summary": brief, "decisions": ["Ship the plane behind approval."],
    "actions": actions, "checklist": actions, "avatar_id": "cedric",
    "org_id": org, "meeting_url": "https://meet.google.com/openclaw-demo",
    "meeting_type": "working_session", "readiness_score": 82,
}, org_id=org)
print(actions[0]["typed"])
PY
```

Expected output — the whole point of the demo, before any UI is opened:

```
{'type': 'notion.create_page',
 'args': {'title': 'OpenClaw Demo',
          'content': 'Meeting summary\n\n…\n\nNext steps\n\n- [ ] …'}}
```

Seed a second meeting with a different title to show grouping.

---

## Environment variables

No values here — set them in `.env` or the App Runner service. **No secrets in
git** (hard constraint 3).

### Always

| Variable | Purpose |
|---|---|
| `LAURA_STORE_PATH` | SQLite store location. A demo-only path keeps resets to one `rm`. |
| `BRAIN_PROVIDER` | `stub` for the key-free demo. |
| `EMBEDDING_PROVIDER` | `hash` for the key-free demo. |
| `PUBLIC_BASE_URL` | Only needed once a real meeting bot must reach you. |

### Demo A — connected-app execution

| Variable | Purpose |
|---|---|
| `PIPEDREAM_PROJECT_ID` · `PIPEDREAM_CLIENT_ID` · `PIPEDREAM_CLIENT_SECRET` | Connect credentials. All three must be set or the whole Pipedream plane stays inert. |
| `PIPEDREAM_ENVIRONMENT` | `development` or `production`. **Accounts do not migrate between the two** — connect again after flipping. |
| `PIPEDREAM_EXECUTOR` | `true`. Off means no connected-app execution at all. |
| `NATIVE_EXECUTOR` | `true`. Also what makes the dashboard show "Approve & run". |

### Demo B — Chat

| Variable | Purpose |
|---|---|
| `OPENCLAW_EXPERIMENT_ENABLED` | `true`. |
| `OPENCLAW_EXPERIMENT_ORGS` | The workspace's `org_id`, or `*`. **Read the warning above before setting this.** |
| `OPENCLAW_GATEWAY_URL` | Required for replies. Without it Chat answers "OpenClaw gateway is not configured". |
| `OPENCLAW_GATEWAY_TOKEN` | Bearer token, if the gateway wants one. |
| `OPENCLAW_AGENT_ID` | Agent id header. Defaults to `laura-executor-test`. |
| `OPENCLAW_AUTO_RUN` | Leave `false`. It cannot bypass approval, but it starts runs you did not ask for. |

### Optional — connected-app policy

| Variable | Purpose |
|---|---|
| `CONNECTED_APP_ALLOW` | Non-empty ⇒ only these slugs may use the generic API plane. |
| `CONNECTED_APP_DENY` | `stripe, github:DELETE *`. Commas separate rules; an operation rule contains a space. |
| `CONNECTED_APP_REGISTRY_EXTRA` | JSON adding an app's API surface without a deploy. |

None of these can waive approval. They only ever remove capability.

---

## The demo

### Demo A — meeting ask to verified page

**Meeting prompt** (say it to the avatar, or seed it as above):

> Create a Notion page called OpenClaw Demo, add a short meeting summary, and
> add a checklist with the next three steps.

Then open **Action Center**.

| Step | Expected UI |
|---|---|
| 1 | Sidebar badge shows the pending count. Tabs read **Needs attention (N)** · In progress · Completed. |
| 2 | Actions are grouped under **MEETING → Working session**, with avatar, time, action count and the meeting summary. A second meeting is its own group. |
| 3 | The card shows a `READY FOR APPROVAL` chip, the ask, and "Ready for your approval — nothing runs without you." |
| 4 | Controls: **Review & approve** · **Add details** · **Discard**, plus **View details ▾** to expand the typed parameters. |
| 5 | Per meeting: **Discuss in Chat**, which opens a chat already bound to it. |
| 6 | Click Review & approve → the row moves to **Completed** with a receipt line ending `· verified · https://www.notion.so/<page-id>`. |

What must be true, and is asserted in `test_openclaw_demo_flow.py`:

* the captured type is `notion.create_page` with **only** `title` and `content`
  — no `start`, `end`, `attendees`, `date`, `description`, `event_id`,
  `response`;
* **zero** vendor calls before approval;
* approval performs **exactly one** POST to `https://api.notion.com/v1/pages`;
* a read-back GET of the created page turns the receipt into evidence;
* a second click replays from the recorded decision — no second page.

### Demo B — chat bound to the meeting

Open **Chat**. A fresh thread opens with exactly:

> If you want to know what I can do, just ask me.

| Control | Behaviour |
|---|---|
| **+ New chat** | Independent threads; the list keeps them all. |
| **CONTEXT** dropdown | Binds the thread to one meeting, or "No specific meeting". |
| **Delete chat** | Confirms, then deletes permanently — thread and messages, not archived. |

**Chat prompt** (in a thread bound to the meeting):

> What did we agree, and can you file the recap?

Expected: it answers from the meeting's distilled summary and decisions, and
proposes a reviewable workflow. The proposal does **not** run: no vendor call,
no run, no decision recorded, until you press Start workflow.

The chat receives summaries, decisions, participants and action parameters —
**never the raw transcript** (hard constraint 6).

---

## Testing with the real connected Notion account

1. Set the three `PIPEDREAM_*` credentials and `PIPEDREAM_EXECUTOR=true`.
   Check `PIPEDREAM_ENVIRONMENT` matches where the account is connected.
2. Dashboard → **Connections** → connect **Notion** through the hosted Connect
   link. Grant it access to at least one page or database.
3. Confirm the workspace sees it: Connections shows a Notion card, and the
   avatar's meeting brief lists Notion among its tools. Neither needs
   Pipedream's paid action catalog.
4. Run demo A. **It writes a real page.** With no parent given it lands at the
   **private workspace root** of the connected account — that is the schema
   default, not a guess. If you want it inside a specific page, say so in the
   ask ("… in the Team Space page") so `parent` is filled.
5. Verify in Notion, then check the Action Center receipt carries the same page
   URL and the `verified` marker.

Demo hygiene: delete the created pages between runs, and remember every run
writes for real once Notion is connected.

---

## Known live limitations

**Blocking, know before demoing**

* **Chat needs a gateway.** No `OPENCLAW_GATEWAY_URL` ⇒ every message answers
  "OpenClaw gateway is not configured". Threads, New chat, Delete chat and
  meeting binding all work without it; only replies do not.
* **The two demos need different flags** — see the table at the top.

**Rough edges**

* The Brain tab requests `/dashboard/knowledge/sources`, which is only mounted
  when the Company Brain is enabled. Locally that is a console 404 on every
  dashboard load. Cosmetic, pre-existing at `0de00fe`, unrelated to this work.
* The **structured** receipt (`verified`, `verification`, `route` as fields) is
  written on the Postgres control plane only. In the SQLite mode the demo runs
  in, the same evidence appears in the human receipt line.
* Pipedream's **pre-built action catalog is plan-gated**. Nothing in this branch
  depends on it, but the Connections "What can I do?" expander is built on it
  and will show the honest "needs the tool-calling tier" message.
* **Linear and monday.com** expose a single GraphQL endpoint, so a read and a
  write are the same HTTP operation. Planner reads are unavailable for them by
  design; the planner says so instead of guessing.
* An app with **no registered API host** has no generic path. It stays listed
  with the reason, and only its deterministic actions run. Adding one is a row
  in `app/actions/app_registry.py` or a `CONNECTED_APP_REGISTRY_EXTRA` entry.
* The SQLite store is **ephemeral on App Runner**. Locally it is a file; treat
  a deleted file as a reset demo.

---

## Before an AWS development deployment

Not done here — this branch has never been pushed or deployed.

1. **Merge order.** This branch carries the backend integration *and* the
   dashboard UX. Reconcile it with the mirror integration first; compare tree
   hashes, and expect `backend/app/brain/tool_registry.py` to be the one file
   where two reasonable resolutions can differ.
2. **Target `laura-backend-next` only.** `main` deploys there. `frozen/v1` is
   what customers run and must not move.
3. **Gate on `/health` `active_sessions == 0`.** Never deploy over a live
   meeting.
4. **Secrets to SSM `/laura/prod/*`**, never to git. An `update-service`
   replaces the whole env map — describe, merge, then update, or you will drop
   variables you did not mean to touch.
5. **Decide the OpenClaw flags per workspace.** Enabling the experiment for an
   org changes its execution route; do not enable it broadly without a gateway
   deployed and reachable.
6. **Run the Postgres control plane** if you want structured receipts and
   durable multi-tenancy; `backend/alembic/` holds the migrations.
7. **Rotate anything exposed** before pointing a real workspace at it.
8. **Confirm the meter is off** after any test call: sessions must be ended, or
   Recall keeps billing per minute.
