# Demo E2E — runbook & checklist (owner walkthrough)

Repeatable script to validate the complete user journey before a demo.
Sanitized: no tokens, no real customer data — test accounts and the staging
workspace only. Billing stays in **sandbox** (`BILLING_ENABLED=false` until
the billing train is merged and flipped deliberately).

**Environments**

| Piece | Where |
|---|---|
| Laura backend (prod) | `https://dhfgfe6yw6.eu-central-1.awsapprunner.com` |
| Cedric (staging — Laura points here) | `https://cedric-staging.vercel.app` |
| Cedric (prod — after Ben's promote + env flip) | `https://www.meet-cedric.com` |
| Slack app (staging) | `A0BGHED8Q4A` — Interactivity URL must end in `/api/slack/interactions` (plural!) |
| Test Slack workspace | `T0AT2QWB4C8`, approvals channel `C0BGBG3EQDR` |

**Reset between runs:** end any live session (`GET /health` → `active_sessions:0`),
clear test captures by letting the session close normally, use a fresh meeting
link per run (the per-URL dedup guard blocks rebooking the same link).

---

## 0. Preflight (2 min, no meeting needed)

- [ ] `GET {laura}/health` → `status:ok`, `active_sessions:0`
- [ ] Auth walls (should all REJECT): `POST {cedric}/api/laura/orgs` → 401 ·
      `GET {cedric}/api/laura/context` → 401 · `POST {cedric}/api/slack/interactions` → 401 "invalid signature"
- [ ] Tampered install state rejected: `GET {cedric}/api/slack/install?state=garbage` → 400
- [ ] Slack Interactivity Request URL ends in `/api/slack/interactions` (a missing
      `s` saves fine in Slack but makes every button dead — verified failure mode)

## 1. USER FLOW (browser, test Google account)

- [ ] New user signs in with Google (scopes: openid/email/profile only — no
      Gmail/Drive consent should appear at login)
- [ ] Org resolved/created; dashboard loads
- [ ] Roster shows exactly **Laura + Cedric** (no internal avatars)
- [ ] Overview is clean: no stat tiles, no Recent-meetings table (record lives
      in Meetings), dispatch panel present
- [ ] "Send an avatar to a meeting": invalid URL (`https://evil.example.com`)
      → toast, **no** session started; valid Meet/Zoom/Teams URL passes
- [ ] **Add a brief (optional)** → fill Title/Objective/Participants/Notes →
      Send → in the call, ask the avatar "what's this meeting about?" — it must
      answer from the brief (context.meeting + brief_markdown reached the session)
- [ ] Minutes remaining visible in Usage & billing; Free = 15 lifetime minutes

## 2. CEDRIC FLOW (staging workspace)

- [ ] Avatars → Cedric → **Connect** → "Add to Slack" (channel field is under
      *Advanced / optional*; leaving it empty = approvals land in installer's DM)
- [ ] Complete Slack OAuth → status chip flips to **connected**, workspace id shown
- [ ] Connector catalog loads (list of tools with connected/not-connected state —
      no tokens or secrets anywhere in the UI)
- [ ] **Disconnect** → chip flips to disconnected, button becomes **Reconnect**
      · if Cedric's revoke fails (502): UI KEEPS "connected" and shows
      "Couldn't disconnect. Try again." (never a fake disconnected state)
- [ ] Reconnect → re-install is idempotent (existing org keeps its credentials;
      a workspace already linked to a different org 409s)
- [ ] In a meeting: "Cedric, <do something>" → he confirms out loud he'll handle
      it after the call, **never** claims it's already done
- [ ] Meeting ends → exactly **one** approval card in Slack (DM or chosen channel)
- [ ] **Approve** → connector executes **once** · double-click Approve does NOT
      double-execute · **Reject** → nothing executes
- [ ] Action status returns to Laura (Meetings view shows the outcome chip)

## 3. BILLING SANDBOX (only when the Stripe PR is merged, still test-mode)

- [ ] Free tier: 15 lifetime minutes shared Laura+Cedric; meter counts join→leave
- [ ] One concurrent meeting per org (second dispatch while one is live → clear error)
- [ ] Minutes exhausted → dispatch returns **402** → UI routes to Usage & billing
      with "Upgrade to Solo" CTA
- [ ] Checkout (test card 4242…) → activation happens ONLY after the signed
      webhook lands (a redirect back alone must NOT activate)
- [ ] Solo: 300 min/period, renewal date visible · Customer Portal opens: payment
      method, invoices, cancel-at-period-end (no plan switching in v1)
- [ ] Payment failed / canceled / late webhook events do not re-activate
- [ ] No live Stripe anywhere (sandbox price `laura_solo_monthly`)

## 4. TENANT ISOLATION (two test users, two orgs)

- [ ] Two Google test accounts → two orgs; each sees only its own meetings,
      minutes, connections, actions
- [ ] Two Slack workspaces: each org's approval card lands only in ITS workspace;
      Approve in org A never executes anything for org B
- [ ] Retries/callbacks stay scoped to the correct org

---

## Known human-gated steps (cannot be automated from a session)

1. Google login + OAuth consent clicks (test users are allow-listed in Testing mode).
2. Slack "Allow" on the install screen + Approve/Reject button clicks.
3. Stripe test-checkout card entry.
4. Being present in the actual meeting (speaking the trigger phrases).

## Failure triage — where to look when a step breaks

| Symptom | Ring to inspect |
|---|---|
| Avatar never joins | Laura `/sessions/start` response · Recall bot state |
| Joins but no brief | `context` in the start body → `cedric.inject_brief` · Cedric `/api/laura/context` |
| Captures but no Slack card | Laura→Cedric `/api/laura/events` delivery (bearer + HMAC) |
| Card but buttons dead | Slack Interactivity URL (plural `/interactions`) · signature |
| Approve executes twice | Cedric dedup / action_id correlation |
| No status chip in Laura | Cedric→Laura status callback |
| Wrong org gets anything | tenancy: org resolution (token→org), RLS |
