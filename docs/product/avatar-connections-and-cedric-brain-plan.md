# Avatar connections + Cedric-brain link: plan

_Written 2026-07-10. Grounds on the live dashboard/auth/avatar code in this repo._

## Where we are today (as-is)

**Signup / login.** Google OIDC (`auth.py`), HMAC session cookie, `org_id == user_id`
seam. Private-beta allow-list gates who gets in; the login page now shows a direct
"Continue with Google". After login → `/dashboard`.

**Avatars.** Folders under `avatars/<id>/`. Shown in the dashboard: `laura`, `cedric`,
`duccio` (`sff` is `hidden: true`). Each has knowledge, persona, voice, templates.

**Avatar address (the "email").** Computed in `dashboard._avatar_email(id)` as
`{base}+{id}@{domain}` where `base` = local part of the **first** entry in
`settings.calendar_invite_emails`. Routing back (`avatars.from_invite_email`) reads the
`+tag`; a tag that matches an installed avatar id summons that avatar, otherwise it
falls back to the default avatar. So inviting `…+cedric@…` → Cedric.

**The bug:** `CALENDAR_INVITE_EMAILS` is **not set in App Runner** → the code falls back
to the default `laura.ai.122222@gmail.com`. If that isn't the inbox Gmail-watch actually
monitors, every address shown is wrong (invites won't summon anyone). Fix = set the env
to the real watched inbox; then the `+tag` addresses are automatically correct.

**Connections.** The Avatars view lists Gmail/Calendar/Slack/Drive as **static rows**: 
they're display only, not a real connect/disconnect flow. `drive_folder_id` is the only
per-avatar wiring that exists.

**Actions.** Laura (this repo) *captures* actions in a meeting and hands them off; the
execution ("fa le tasche") lives in **Cedric, the other repo (the brain)**, which owns
the real app integrations. Today the hand-off is capture→approval via meet-cedric.com;
Ben owns the Cedric side (see enterprise-tenancy-plan + the e2e map).

## Where we want to go (to-be)

> A user signs up here with Google → gets their avatars → opens an avatar's **Configure**
> tab → clicks to connect the tools that avatar may use (Gmail, Calendar, Slack, Drive…)
> → for **Cedric**, one of those connections is **"Connect the brain"**, which links this
> avatar to the Cedric repo so the brain executes with its own integrations. Ideally
> signup here also provisions the matching identity on Cedric so the two are pre-linked.

## Workstreams

### 1. Avatar addresses (quick, unblocks the demo)
- Set `CALENDAR_INVITE_EMAILS` in prod to the **real** watched inbox (owner to confirm
  which Gmail/Workspace address that is).
- Decide the scheme: **Laura = the bare base** (no `+laura`, it's the canonical inbox),
  Cedric = `+cedric`, Duccio = `+duccio`. Small change to `_avatar_email` so the primary
  avatar returns the bare base. (Duccio's `+duccio` alias already exists and works.)
- Longer term: if we get a Workspace on a real domain, watch `avatars@ourdomain` and use
  `avatars+cedric@…`: same mechanism, professional address.

### 2. Per-agent "Configure / Connections" section
- New tab on the avatar detail: **Configure** with a live list of connections, each a
  real **Connect / Disconnect** (OAuth) rather than a static row.
- Storage: per-(org, avatar) connection records (provider, scopes, token ref). Tokens in
  SSM/secret store, never in the row. Reuse the `org_id` seam.
- Providers v1: Google (Calendar + Drive + Gmail-send), Slack. Each connection is the
  seam Cedric plugs into; so "what this avatar can touch" is data, not code.

### 3. Cedric-brain link (cross-repo): the core
- Add a **"Connect to Cedric (brain)"** connection in Cedric's Configure tab.
- Define the **contract** with Ben's repo (one page): (a) a server-to-server auth token
  per org; (b) an endpoint on the brain that accepts a *captured action* payload
  `{org_id, avatar_id, meeting_id, action, context}`; (c) a status/callback so the
  dashboard can show "done / failed" without holding a transcript.
- Flow: Laura captures the action → POSTs to the brain endpoint for that org → brain
  executes via its own app integrations → returns status → dashboard shows provenance.
- This is the "Agent Card connectivity" from the enterprise-tenancy plan, made concrete
  for one principal (Cedric) first.

### 4. Auto-provisioning on signup
- On first Google login here: mint `org_id` (done), upsert user (done), and, new,
  fire a server-to-server "provision org on Cedric brain" call so the counterpart exists
  without manual steps. Idempotent; failure is non-fatal (retry later from Configure).

### 5. Invite block redesign
- Replace the current "Invite this address to a meeting to summon <Name>" panel with a
  cleaner component: avatar chip + monospace address + Copy + a one-line "how it works".
- Same data (`a.email`), better presentation; optionally a QR / "add to calendar" helper.

## Sequencing
1. **Now:** #1 (addresses) + #5 (invite redesign): pure this-repo, fast, demo-ready.
2. **Next:** #2 (Configure/Connections UI + storage): this-repo, no Cedric dependency
   for Google/Slack.
3. **Then, with Ben:** #3 (brain contract) → #4 (auto-provision). Blocked on the Cedric
   repo exposing the org-provision + action endpoints.

## Open questions (owner/Ben)
- Real watched inbox for invites? (sets #1)
- Do we have/want a Workspace domain for avatar addresses? (#1 long-term)
- Does the Cedric brain already expose an HTTP action endpoint + per-org token, or is
  that new work on Ben's side? (#3)
- Auth model between the two repos: shared secret per org, mTLS, or OAuth client-creds?
