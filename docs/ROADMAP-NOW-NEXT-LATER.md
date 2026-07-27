# Laura/Cedric — Roadmap: NOW → NEXT → LATER

*2026-07-20. Grounded in the council evaluations and the actual repo state. Two tracks run in parallel: **A = product/commercial**, **B = codebase restructure**. Hard rule from CLAUDE.md applies throughout: never break the live-meeting contract (`ws/<conversation_id>`, `{type:"speak",text}`, `recall_client` signatures), keep the demo key-free, end sessions to stop meters.*

---

## NOW — next 2 weeks (deadline-driven)

### A1. EU AI Act compliance pack — before Aug 2 (≈2 days)
Article 50 applies to Laura (AI interacting with humans, synthetic voice/face) from **2 August 2026**.
- Add a spoken + on-screen disclosure at bot join ("I'm Laura, an AI assistant — this meeting is being processed"). Implement as a config flag in `backend/app/config.py` + a join announcement in the session-start path; render the line in `frontend/talk.html`.
- Mark synthetic audio/video as AI-generated (metadata on `/tts` output; visible label on the avatar page).
- **Zero Recall's recording retention** (default is 7 days — currently contradicts your "no transcript stored" claim). One API setting in `recall_client.py`.
- Write `docs/legal/` one-pager: data flow, subprocessors (Recall, Cerebras, Anthropic, ElevenLabs), retention, consent posture. This is the first document any buyer's IT asks for.

### A2. Ship persistence (≈half day — the pieces already exist)
`etc/litestream.yml` and `scripts/start-with-litestream.sh` are already in the repo; PR #58 is pending. Merge, deploy, then run a **restore drill** (kill the instance, verify org memory survives). Until this ships, the "remembers" pitch is false — it blocks everything commercial.

### A3. Wire the Cedric approve→execute loop end-to-end (≈2–4 days)
The Business-tier differentiator and demo Beat 3. The seams exist (`cedric/callback.py`, `outbox.py`, `executor.py`, `ledger.py`); the gaps are the Slack Interactivity URL (Block Kit Approve button) and Gmail send.
- Definition of done: click **Approve** in Slack → the recap email actually sends → the action is marked executed in the ledger with provenance pointing at the decision that authorized it.
- Do not demo or sell the Business tier until this passes `/smoke-demo`.

### A4. Silent-sidecar mode for external calls (≈1–2 days)
The buyer verdict: never announce "readiness 72/100" in front of a customer.
- Add a mode flag (e.g. `EXTERNAL_CALL_MODE=silent`): `meeting_state.py` already computes missing steps — route the closing intervention and readiness score to the CSM as a **Slack DM in real time** instead of TTS. Keep the speaking avatar for internal meetings.

### A5. Fix the commercial plan (this week, zero code)
- Reclassify SFF Studio as **dogfooding**, not customer #1 — never in revenue metrics.
- Rescope the outreach experiment: **drop the 15 Italian exec-search leads** (English-only regex can't serve them), run the 17 SaaS-CS leads now, and start expanding that list toward 150–250 contacts. Lead with the saved-miss/readiness artifact, not the avatar.
- One narrative: "Laura closes the loop." Cedric is Laura's follow-through, not a second character.

### B1. Repo triage (≈1 day — mechanical, do it before any refactor)
Current state: `backend/` 553 files / 14.4MB, `main.py` **299KB**, `store.py` 112KB, `dashboard.py` 108KB, `brain.py` 90KB, `control_plane.py` 65KB, `config.py` 51KB. Plus ~618 junk files (`.venv/`, `__pycache__/`, `.pytest_cache/`, `.playwright-mcp/`) sitting in the working tree, 31MB in `frontend/` (media + a 173KB `dashboard.html`), and stray artifacts (`chat-tab-working.png` at root, `voice-previews/`).
- Verify `.gitignore` covers `.venv/`, `__pycache__/`, `.pytest_cache/`, `.playwright-mcp/`; `git rm -r --cached` anything tracked.
- Move `chat-tab-working.png` and `voice-previews/` → `docs/assets/` (or delete); large binaries (`laura.glb`, media) → Git LFS or a release bucket if repo size hurts.
- Decide the fate of `lovable/laura-meeting-expert/` (a whole separate bun/Vite app): if it's the future dashboard, promote it and delete the 173KB `frontend/dashboard.html`; if stale, archive it out of the repo. One frontend, not two.

### B2. Freeze behavior with contract tests before restructuring (≈1 day)
- Write/verify tests that pin the live-meeting contract (`ws/<conversation_id>` messages, `{type:"speak",text}`, `recall_client` webhook shapes) and the artifact schema from `POST /sessions/{id}/end`. `tests/` has only ~43 small files for a 550-file backend — the contract tests are the refactoring safety net.
- Run `/test-backend` + `/smoke-demo` green before and after every restructure PR; use the `code-reviewer` agent on each.

---

## NEXT — weeks 2–6

### A6. Multi-tenancy (blocks any second external customer)
Per-tenant tokens and data isolation. Seams exist (`auth.py`, `entitlements.py`, `scripts/validate_rls.py`, Postgres/RLS docs in `docs/infra/MULTI-TENANCY*.md`). Sequence: per-tenant API keys → tenant-scoped storage → the Postgres/RLS migration from the existing plan.

### A7. Upgrade the tracker: regex → streaming LLM classifier
`meeting_state.py` (28KB of English regex) is the moat-bearing component and its weakest tech. Add a per-utterance streaming classifier (Cerebras is fast/cheap enough) that maps transcript lines to template steps; keep regex as the zero-latency fast path and for the deterministic closing intervention. This is what makes templates author-able by customers and unlocks non-English (the Italy track).

### A8. 3–5 unaffiliated design partners
$500–1,000/mo, 90 days, sourced from warm network + CS communities. The deliverable that earns full price: **one documented saved miss** (caught DPA/security-review gap) per partner.

### A9. Validate the readiness score
Run Laura silently on 20–30 of a design partner's real onboarding calls; measure correlation between readiness score and go-live slippage. The correlation chart becomes the sales deck's centerpiece — and for the v3 pipeline, track **% of proposed actions approved without edit**.

### A10. De-risk Recall
Get Output Media pricing **in writing** (their `web_4_core` is $0.60/hr, `web_gpu` $1.50/hr — potentially doubling COGS and breaking the 86–92% margin claims); re-run `docs/finance/cost-model.md` with the real number. Keep `recall_client.py` a clean seam so a MeetingBaaS/Skribby-class swap stays a vendor change, not a rewrite. Build the bot-blocked fallback before Teams' `BlockDetectedBots` rollout (Aug 2026), and add "bot admitted?" to the readiness check.

### B3. Restructure the backend (mechanical, in this order)
Target layout — moves only, no behavior change, re-export shims during transition:

```
backend/app/
  api/            # routers split OUT of main.py: sessions, webhooks, org, dashboard, tts, billing
  core/           # config, auth, security, crypto, entitlements
  meeting/        # meeting_state, decision, end_of_turn, emotion
  brain/          # brain, llm, rag, embeddings, tools, tool_registry
  integrations/   # recall_client, google_client, asana_client, granola_client, drive_client, gemini_ears, anam_client(legacy)
  actions/        # outbox(_pg), executor, ledger, actions, action_plane, action_deps, action_reconcile, autopilot, scheduler
  cedric/         # (already a package)
  knowledge/      # (already a package)
  persistence/    # store, billing storage
```

1. **Split `main.py` (299KB) into APIRouters** under `api/`, leaving `main.py` as <500 lines of app assembly (mount routers, lifespan, middleware). Biggest single win for navigability.
2. Move modules into the packages above with `from app.x import *` shims for one release; delete shims after.
3. Then split the remaining monsters along existing seams: `store.py` (112KB) per domain (sessions/artifacts/ledger/orgs), `dashboard.py` (108KB → most of it is HTML — move markup to `frontend/` or templates), `brain.py` (90KB), `control_plane.py` (65KB).
4. Candidate dead code to verify-then-delete (grep for imports first): `anam_client.py` (legacy vendor, abandoned path), `emotion.py`, `gemini_ears.py` if unused in prod config.
5. Each step: one PR, `/test-backend` + `/smoke-demo` green, `code-reviewer` agent pass. Never mix a move-PR with a behavior-PR.

### B4. Docs consolidation (≈half day)
108 files in `docs/`. Enforce the repo's own rule (`REPO_HYGIENE.md`): keep the declared single-sources-of-truth (`docs/product/WEDGE.md`, `docs/ARCHITECTURE_CURRENT.md`, `docs/finance/cost-model.md`) current; everything superseded moves to `docs/archive/` with a one-line index in `docs/README.md`. Delete duplicates rather than letting two "current" architecture docs drift (this already bit the cost model once).

### B5. Repo boundaries
Keep the mono-repo, but make each sub-project self-describing with a one-line "status: active/stale" header in its README: `extensions/laura-meet` (Chrome ext), `relay/laura-ears` (Cloudflare worker), `gpu/` (photoreal), `lovable/` (per B1 decision). Anything stale for >2 months moves to `archive/` or its own frozen repo.

---

## LATER — 6+ weeks (gated on design-partner traction)

- **A11. Browser guidance, sandboxed first.** The "let me show you where to click in Asana" feature — build it on a **demo tenant via screen share** (`asana_client.py` already exists as the integration seam), never on live customer data initially. Live in-customer-tenant guidance only after multi-tenancy + a security posture that survives review; treat computer-use reliability as experimental.
- **A12. Team/company avatar personalization** — this is onboarding configuration on the existing folder-per-avatar platform, not new product. Ship as a setup flow (upload knowledge pack, pick voice/face), not as new architecture.
- **A13. CRM push (HubSpot/Salesforce) + task-tool push (Linear/Asana/Jira)** — per the existing commercial roadmap, on Cedric's gated connector pattern; sequence by what design partners actually ask for.
- **A14. Analytics dashboard** (talk-share, action-completion rate, readiness trend) — sells to managers; needs the ledger data that persistence (A2) makes durable.
- **A15. Photoreal face** — keep parked. The council's read: the face is a demo hook and a growing liability in compliance-sensitive calls; spend nothing here until customers ask.

---

## The one-line sequencing rule

**Compliance pack → persistence → approve-loop → silent sidecar → outreach (all NOW) → tenancy + LLM tracker + design partners (NEXT) → browser guidance and everything else (LATER).** Every new feature idea gets tested against: "does this help the first 3 unaffiliated customers approve actions on real meetings?" If not, it's LATER.
