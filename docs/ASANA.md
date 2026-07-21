# Asana integration, Petra, the project-manager avatar

The first task-tool connector ("Task-tool push, actions become tickets" on
the commercial roadmap): the org's Asana workspace becomes something an
avatar can **read** (a live snapshot when she joins a meeting) and **write**
(meeting action items become Asana tasks). It ships with **Petra**
(`avatars/petra/`), an AI project manager grounded in an original PM
knowledge pack; but the integration is avatar-agnostic: any avatar with the
`asana` capability gets the same powers.

## What it does

1. **Snapshot at join.** When a session starts for an eligible avatar, the
   backend fetches a compact workspace brief, projects, open tasks, owners,
   due dates, an OVERDUE flag, and injects it into the avatar's meeting
   context (the same `memory_brief` channel the Drive folder and tool
   registry use). In the call, "Petra, what's overdue on Onboarding?"
   answers from live workspace facts. TTL-cached (10 min) and capped
   (~2.4 KB) so joins stay fast and the prompt stays cheap.
2. **Action items become tasks.** At meeting end, the typed-action producer
   (`brain.type_actions`) may annotate each captured action item with an
   `asana.create_task` spec; name from the item itself, assignee only if an
   email literally appears in the item, due date only if a concrete date was
   stated, project only if its name appears in the item/summary. Items that
   map to calendar/email keep those types; Asana is the fallback for every
   other genuine piece of work.
3. **Approval-gated by default.** Typed actions appear on the dashboard with
   the existing **Approve & run / Reject** controls; approving executes the
   write via the native executor and attaches the Asana task URL as the
   clickable receipt. Rejecting kills it (terminal, cannot be executed later).
4. **Optional auto-push.** `ASANA_AUTO_EXECUTE=true` executes the typed
   `asana.*` actions **at finalize**, without waiting for approval; the
   one-toggle "make it automatic" mode. Receipts land in the same provenance
   channel, so the dashboard rows still show Done ↗ with the task link.
   Only Asana types auto-push; calendar/email always keep human approval.

## Setup

**Option A; one-click OAuth (the "Connect Asana" button, recommended).**
One-time platform setup, then every owner connects with two clicks:

1. Create the OAuth app (any Asana account, ~5 min):
   [app.asana.com/0/my-apps](https://app.asana.com/0/my-apps) → *Create new
   app* → add the redirect URL `{PUBLIC_BASE_URL}/oauth/asana/callback` →
   enable full/default scope → copy the client id + secret.
2. Set on the deployment:
   ```
   ASANA_CLIENT_ID=<from the app>
   ASANA_CLIENT_SECRET=<from the app>
   ```
3. The Connections card now shows **Connect Asana**: clicking it sends the
   owner to Asana's own sign-in/consent screen and back; connected. The
   grant (a refresh token) is stored encrypted per-org
   (provider="asana-oauth") and minted into short-lived access tokens
   (cached ~1h) on use; a single-use signed state + HttpOnly cookie bind the
   round-trip to the browser that started it (same CSRF machinery as the
   Google flow).

**Option B: Personal Access Token (no OAuth app needed).**

1. **Get a token**: Asana → your avatar (top right) → Settings → Apps →
   *Developer apps* → **Create personal access token**. Copy it once.
2. **Connect** (any one of these):
   - **Dashboard**: Connections view → **Asana** card → paste the token →
     Connect (the card shows the paste field whenever the OAuth app env is
     not configured). The backend verifies it live against Asana before
     storing it (encrypted per-org, same vault as the Google refresh token);
     a bad token is a clean error, and the token is never echoed back to the
     browser. Disconnect from the same card (clears OAuth grant AND PAT).
   - Single-tenant env vars on the deployment:
     ```
     ASANA_TOKEN=<the PAT>
     ASANA_WORKSPACE_GID=          # optional; auto-discovered when blank
     ```
   - Programmatic (what the dashboard button does under the hood):
     ```python
     store.set_org_oauth(org_id, pat, provider="asana")
     ```

Credential precedence at call time: OAuth grant → pasted PAT → env token.
3. **Enable writes**: the write path rides the native executor: 
   `NATIVE_EXECUTOR=true` (already required for calendar/email execution).
4. **Optional**: `ASANA_AUTO_EXECUTE=true` for approval-free task pushing.

No token anywhere ⇒ every Asana feature is silently off; the key-free demo
and all other avatars are untouched.

## Eligibility (which avatar may touch Asana)

`main._avatar_asana_enabled(org, avatar)`: the org must be connected (per-org
row or env token) AND the avatar's per-avatar **`asana` capability toggle**
must not be explicitly off; default ON when connected, same rule as the
`google` toggle (`store.capability_enabled`). The dashboard approve door
enforces the same family toggle per action (`executor.capability_family`),
so an avatar with Google off can still push tasks and vice versa.

## Action types (executor)

| Type | Args | Receipt |
|---|---|---|
| `asana.create_task` | `name` (req), `notes?`, `project?` (gid or name), `assignee?` (email/gid), `due_on?` (YYYY-MM-DD) | task permalink |
| `asana.update_task` | `task` (gid, req), `completed?`, `due_on?`, `assignee?`, `name?` | task permalink |
| `asana.add_comment` | `task` (gid, req), `text` (req) | task permalink |

All three follow the `google_client` contract: soft `{"ok": False, "error"}`
returns, never raise, never log tokens or task content. The typed-action
producer only ever emits `create_task` (update/comment need a task gid, which
a meeting sentence can't safely provide); the other two are for programmatic
callers and future voice-capture flows.

## Grounding rules (why a hallucination can't reach your board)

`brain._sanitize_typed` re-validates every model-proposed spec:
- task **name** falls back to the action item's own text (grounded by
  construction);
- **assignee** must be an email that literally appears in the item's
  distilled source (a bare "Marco" is not resolvable; task stays unassigned);
- **due_on** must be ISO-date-shaped;
- **project** must literally appear in the item/summary text, else dropped -
  a misfiled task in a real board is worse than an inbox task.

## Petra (`avatars/petra/`)

Wake word "Petra". Persona: calm senior PM; keeps owners/dates honest,
answers grounded in the snapshot, says "captured for the board" (never "done")
for in-meeting asks. Knowledge pack (original, audit-safe; drop your own
methodology docs alongside and re-run ingest):
fundamentals (iron triangle, charter, WBS) · agile/scrum · risk & RAID ·
estimation & planning · status meetings & reporting · stakeholders & RACI.
Process template `project_status_review` powers MeetingState tracking +
the one closing intervention (e.g. a blocker left without an owner).

## Security & PII

- Tokens: encrypted at rest (per-org row) or in deployment env/SSM; never
  in git, never logged (`.claude/hooks/guard.py` + the client contract).
- The workspace brief carries task/project names, owners, and dates only -
  and, like every brief, lives in memory + prompt, never in logs.
- Writes are only ever: distilled action items a human saw (approval mode)
  or agreed in the meeting (auto mode, explicit opt-in); never raw
  transcript content.

## Testing

`backend/tests/test_asana.py`: key-free, Asana mocked at the HTTP layer:
client reads/writes/soft-failures, brief building + caps, executor dispatch
+ receipts + auto-push, producer grounding, eligibility, and the Petra
avatar itself. Run: `python -m pytest tests/test_asana.py` from `backend/`.

## Limitations / next steps

- Typeahead search (`find_tasks`) uses Asana's typeahead endpoint; good for
  name lookups, not full-text search (that's a paid Asana tier API).
- No webhook sync back from Asana (board changes appear at the next
  snapshot, ≤10 min).
- OAuth "Connect Asana" button (replacing the PAT) is the natural upgrade
  when this goes multi-customer; the client's token resolution already has
  the seam (`_token`).
