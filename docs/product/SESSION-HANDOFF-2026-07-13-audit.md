# Session handoff — audit-driven production hardening (2026-07-13)

A single session that ran an adversarial audit of every user-facing flow, then
fixed the confirmed defects in reviewed, deploy-verified batches. This is the
durable record: what shipped, what's documented-but-not-fixed (and why), and the
human-gated finish line.

## Shipped today (all merged to `main`, CI-green, session-guarded, deploy-verified)

| PR | What | Notes |
|----|------|-------|
| #146 | RLS isolation validated on real Postgres (`scripts/validate_rls.py`) | The NOW-4 safety thesis — FORCE-RLS isolates a non-superuser org even on a forgotten WHERE; WITH CHECK blocks cross-org writes. Never exercised by the SQLite suite. |
| #147 | Per-org agents — the `org_id` seam made real (SFF Studio) | `@sffstudio.com` login → `org_sff` → sees only `[laura, cedric]`; everyone else unchanged. Review caught a shared-org brain-connect 404 blocker. |
| #148 | 3 money-safety meter leaks | Dup-bot dedup on `/sessions/start` + per-meeting lock; platform-aware guards (Zoom/Teams, not just Meet); `leave_call` retry+raise + a persisted `leave_pending` retry so a 5xx/401 never strands the meter. Review caught 3 blockers (4xx-as-stopped, unpersisted flag, forever-retry). |
| #149 | 6 robustness defects | Live mid-stream LLM drop → recovery line + 200 (no re-deliver); post-meeting error → degraded stub → bare scaffold (deliverable never lost); `/redeliver` fire-and-forget (no 504); roster stops hardcoding 'laura'; `/deliver` correct avatar name; demo 404 not 500. |
| #150 | 3 live-interaction improvements | Self-introduction at the first natural pause (never over a human — review caught a talk-over blocker); interjection floor re-checked at speak-time so she doesn't talk over lagging partials; honest "answering generally" caveat when a process question falls below the grounding floor. All config-gated, default on. |

Every behavior change in #147–#150 was adversarially reviewed before merge; each
review caught at least one real blocker that was fixed pre-merge.

## The audit: 28 adversarially-confirmed defects

Full repro + fix for each is in [`FLOW-AUDIT-2026-07-13.md`](FLOW-AUDIT-2026-07-13.md)
(9 flows × map→find→verify, 58 agents, 11 raw findings refuted). Status:

**6 demo-blockers:**
- **Dup-bot / double-meter** (×2) → FIXED (#148).
- **Cross-tenant leak: auto-join pooled into the Demo org, visible to every logged-in user** (×2) → **NOT a code patch — architecturally human-gated.** Investigated: all three auto-join paths (calendar, Gmail, Cedric bearer) are single-global-account with no connection→org data to resolve a real owner, and deriving org from attendees is forbidden (spoof hole). The only change shippable today (drop `demo_org` from the logged-in sentinels) would break the owner's *own* dashboard and reverse a deliberate, tested beta choice. This IS the real tenancy-isolation track: Postgres/RLS + per-org connect flows + Cedric-as-principal — see below.
- **"Add to Slack" full-page-navigates to a JSON blob** (×2) → reported to PR **#133** (its session owns `frontend/dashboard.html`).

**19 rough-edges + 3 polish:** the live/post-meeting/roster/demo ones → FIXED (#149, #150). Remaining, by decreasing value:

## Not-yet-shipped (re-doable follow-ups, findings preserved in FLOW-AUDIT)

1. **Dashboard-accuracy batch** (implemented once, lost to a scratchpad cleanup before commit — re-doable in one pass from the FLOW-AUDIT specs):
   - `actions_30d` sums action lists capped at 12 → undercounts the headline; use a true per-row count.
   - Meetings table silently shows newest 60 while the tile counts all → expose `meetings_total` + raise the cap (frontend "N of M" indicator is #133's).
   - Billed `duration_seconds` from transcript span, not bot uptime → prefer Recall `status_changes` join→terminal timestamps (a bounded off-hot-path `get_bot` fetch), never 0 for a bot that ran. *(Low urgency: these bite at scale, not in a <60-meeting beta; billing is a dashboard estimate, not live charging.)*
2. **Orphan-on-restart reconcile** (money-safety backstop): an App Runner redeploy (every `main` push) with a live bot orphans the meter AND `/health` shows a false `active_sessions:0`. **Largely subsumed by the durable-store fix** (with a persistent store, sessions survive restarts → no orphan) and **already mitigated** by the session-guard we run before every deploy (`active_sessions:0` check). Recommended backstop when durable store lands: a boot sweep that re-adopts Recall's non-terminal bots so reconcile finalizes them.
3. **Onboarding leads dropped**: a Google-verified email that isn't allow-listed is shown a "we noted your interest" waitlist that stores nothing and sends nothing. Either capture the lead to a waitlist table or make the copy truthful (a real contact path). *(Product decision on lead handling + `frontend/login.html` copy.)*

## The human-gated finish line

Code is as far as it can go without these. Nothing below is fixable in the repo alone.

**👤 Duccio:**
- **Supabase Postgres + apply Alembic `0001` (RLS) + point `LAURA_STORE_PATH` at it.** This one unlocks the most: real tenant isolation (closes the cross-tenant leak), a durable store (kills the orphan-on-restart + wiped-archive-on-deploy class), and per-org connections. The migration + RLS is validated (#146). *(Note: `0001` needs `pgcrypto`+`citext`; `pgcrypto` is redundant on PG13+ — a trivial cleanup.)*
- **Publish/verify the Google OAuth app** (it's in Testing mode → an allow-listed customer who isn't also a Google *test user* hits "Access blocked" on first login).
- **DNS**: the 3 CNAMEs for `app.lauravatar.com`.
- **Pricing**: lock the per-seat / per-minute number (finance model exists).
- **A live multi-person test call** to calibrate the new turn-taking (self-intro delay, interjection floor, deference) against real Recall latency.

**🧑‍💻 Ben (Cedric):**
- Per-workspace bearer + the Slack Interactivity URL so Cedric-initiated dispatch resolves a real org (today the machine bearer is single+global → `demo_org`).
- Gmail-send wiring for real outbound follow-ups.

## Guardrails that held all session
Live-meeting contract untouched; demo stays key-free; no secrets in git; the
per-minute meter is now *more* protected (#148); transcripts never logged; latency
off the hot path; session-guard (`active_sessions:0` + no in-flight App Runner op)
as its own step before every one of the ~7 deploys; adversarial review before every
non-trivial merge. Coordinated with the avatar session (#130 emotion, #133 dashboard)
by staying out of their files and reporting findings to them.
