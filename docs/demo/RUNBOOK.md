# Scripted demo — RUNBOOK (key-free, YC application/video)

A controlled, deterministic, **key-free** 10-step demo that proves the whole
Laura value loop: she joins a meeting, answers grounded from a company doc with
a citation, captures a task, asks for the missing detail, and the dashboard shows
the task move captured → needs-details → proposed → approved → completed with a
time-saved tally.

**Why key-free.** With no `ANTHROPIC_API_KEY`, the brain transparently falls back
to the offline `stub` (extractive: it quotes the top-matching process chunk and
names the source) and `EMBEDDING_PROVIDER` defaults to `hash`. The retrieve →
answer → cite → capture → artifact pipeline runs for free, offline, and
**deterministically** — the same inputs give byte-for-byte the same answer every
run, so the demo is safe to script and safe to record. This is exactly the path
the CI guard exercises: `backend/tests/test_demo_e2e.py`
(`./scripts/run_demo_e2e.sh`).

**What the CI guard covers (be precise on camera).** Steps 2-6 — sample →
grounded-answer-with-citation → post-meeting artifact — are asserted **end-to-end
through the real endpoints**: if one breaks, the test goes red. Steps 7 and 10 —
the approve door and the time-saved stat tiles — are guarded at the **contract
surface**: the test proves the `POST /org/actions/{id}/approve` route and its
gate still exist and that every advertised `/dashboard/summary` stat key is
present, so a rename or a dropped tile turns the build red. What the test does
**not** run key-free is a full authenticated approve + external execute (steps
8-9) — those are **presenter-verified** live against a dashboard that already
has data. So: steps 2-6 CI-guarded end-to-end; steps 7 & 10 CI-guarded at the
contract; steps 8-9 presenter-verified.

**Real vs stubbed in key-free mode** (call this out honestly on camera if asked):

| Part of the loop | Key-free demo | With a real key (prod) |
|---|---|---|
| Retrieval / grounding (which doc, which section) | **REAL** (hash embedder) | real (vector embedder) |
| The spoken answer | **stubbed** — extractive quote of the top chunk, not a paraphrase | model-written, conversational |
| Citation / source shown | **REAL** — `citations` + `retrieved` | real |
| Post-meeting artifact (summary, actions, decisions, risks, readiness, missing steps) | **REAL structure**, keyword/heuristic + process-template derived | model-written summary, same structure |
| Action capture + lifecycle fields (`item`/`owner`/`gap_type`) | **REAL** | real |
| Task **execution** on approve | **stubbed** (no external write; a receipt is recorded) | real Pipedream/native write |

Everything the audience sees on the loop is real *plumbing*; only the prose and
the final external write are stubbed without a key.

---

## Setup (before you present)

```bash
# 1 · Prove the loop is green (offline, no keys). ~3s.
./scripts/run_demo_e2e.sh          # → RESULT: PASS

# 2 · Start the backend key-free for the live browser demo.
cd backend && BRAIN_PROVIDER=stub EMBEDDING_PROVIDER=hash \
  "$HOME"/venvs/laura/bin/python -m uvicorn app.main:app --port 8000
# Demo console:  http://127.0.0.1:8000/           (the /demo page)
# Dashboard:     http://127.0.0.1:8000/dashboard  (owner view)
```

Question pre-filled in the demo console:
**"What approvals are required before IT provisions access?"**
Sample transcript loaded by the **Load sample** button:
`avatars/laura/sample_meeting.txt` (a customer-onboarding kickoff).

---

## The 10-step script

Each step: **[click/command]** → *what you say* → **expected on-screen**.
The (REAL) / (STUBBED) tag marks what is genuinely live vs. simulated key-free.

### 1 · Open the dashboard, run the System Check (REAL)
**[click]** Open the dashboard and open the **System Check** page.
*"Before a demo I run Laura's System Check — one page that pings every
dependency. Green across the board means she's ready to be called into a
meeting."*
**Expected:** the **System Check** page shows all checks green.
> The **System Check** page ships in a sibling PR — this runbook only references
> it by name and does not depend on its code. If it isn't merged yet, substitute
> `GET /health` → `{"status":"ok","active_sessions":0}`.

### 2 · Laura joins a short meeting (REAL structure / STUBBED transcript)
**[click]** In the demo console's **Post-meeting artifact** panel, click **Load
sample** to load the onboarding kickoff transcript. *(In prod she is dialed in
live via Recall; here we replay a real captured transcript so the demo is
deterministic.)*
*"Laura gets invited to a customer-onboarding kickoff — Priya from our side,
Daniel from the customer, and Laura in the room."*
**Expected:** the transcript box fills with a short multi-speaker meeting
(`Priya:` / `Daniel:` / `Laura:` turns), targeting an **August 4** go-live.
*(Endpoint: `GET /demo/sample`.)*

### 3 · Ask a question, answered FROM the company doc, WITH a source (REAL grounding + citation)
**[click]** In the **Ask the avatar** panel, keep the pre-filled question
*"What approvals are required before IT provisions access?"* and click **Ask**.
*"You can ask Laura anything mid-meeting. She only answers from your process
docs — and she cites the one she used."*
**Expected:** the answer begins **"Per onboarding_sop.md (Approvals required):
…"** and quotes the SOP's approval steps (hiring-manager approval before IT
provisions; Security-lead approval for elevated access). A **source chip** shows
`onboarding_sop.md`, confidence is high, and it's marked grounded.
*(Endpoint: `POST /demo/ask` → `answer`, `citations:["onboarding_sop.md"]`,
`retrieved:[{source,section,score}]`, `sufficient_context:true`.)*
> Honesty beat (optional): ask something the docs don't cover ("gold price
> yesterday?") — she returns **no citation** and says she can't answer from the
> docs. "From your docs" always means it.

### 4 · Laura captures the next-steps from the room (REAL capture)
*"Now watch her act. The room throws out concrete next-steps — Daniel says
'we still need to book the security assessment' and 'we'll name the
implementation owner internally.' Laura captures those as action items rather
than pretending they're already done."*
Both of those lines are in the shipped transcript, so they appear as real
captured rows. **Expected (in step 6's artifact):** those next-steps show up in
`actions[]`, owner **UNASSIGNED** until named.
> Honesty beat: heuristic capture is imperfect key-free — some captured rows are
> *question* lines (e.g. "the security review — where do we stand on that?"),
> not imperatives. Call that out if asked; a real brain filters actions down to
> genuine to-dos. If you want a punchier on-camera ask you may *say* an
> illustrative line like "Laura, email Daniel the security checklist," but that
> exact wording is **not** in the shipped transcript and won't appear as a row —
> the rows come from the real lines above.

### 5 · Laura asks for the missing detail (REAL gap detection)
*"She doesn't invent an email address or a deadline. The transcript itself has
gaps — no signed DPA, no security review booked, and no named implementation
owner — so she flags exactly what's missing."*
**Expected (visible in step 6's artifact):** `missing_steps` includes
`security_approval`, `dpa_confirmation`, and **`implementation_owner`** — the
detail she'd ask for before executing. Every action carries a **`gap_type`**
field; the unnamed-owner ask gets `gap_type: "owner"` (the beat that drives the
"needs details" state), while lines with no missing slot read `"none"`.

### 6 · Task appears in the dashboard: needs-details → proposed (REAL)
**[click]** Click **Analyze meeting** in the console (`POST /demo/post_meeting`),
then open the meeting in the **dashboard**.
*"The meeting becomes a structured artifact — a summary, the risks, and every
action she captured, each with an owner slot and the gap that still needs
filling."*
**Expected:** the artifact renders `summary`, `risks`, and `actions[]` (alias
`checklist`), each action showing `item` / `owner` / `gap_type`. An action whose
gap is unfilled shows **needs-details**; once the gap is filled it reads
**proposed**. A readiness tile shows the onboarding **readiness score**
(**40/100** for this meeting — several required steps still open).
> Key-free the `decisions` list comes back **empty** for this short transcript —
> the offline heuristic extractor is conservative and records no explicit
> decision (with a real key the "commit to August 4" line surfaces as one). So
> don't point at a Decisions section on camera in stub mode; lead with the
> summary, risks, and actions.

### 7 · Approve the task (REAL approve door)
**[click]** On a proposed action, click **Approve**.
*"Nothing executes until a human approves. One click."*
**Expected:** the action flips to **approved** in the ledger; the row shows an
approved state. *(Route: `POST /org/actions/{id}/approve` with
`{"decision":"approve"}` — the same door the Northstar MVP e2e drives.)*

### 8 · Laura executes (STUBBED key-free)
*"On approval Laura executes — sends the email, creates the task, books the
event."*
**Expected (key-free):** the action is marked done with a **recorded receipt**;
**no real external write happens without a key** — say this plainly. With a real
key + a connected Pipedream/Google account this is the genuine send.

### 9 · Laura verifies (STUBBED key-free / REAL provenance)
*"And she verifies the result — every action carries a provenance trail:
captured → approved → executed, with who and when."*
**Expected:** the action's execution provenance shows a terminal
**done/completed** state with its receipt. (Key-free the receipt is synthetic;
the provenance record itself is real.)

### 10 · Dashboard shows the tally + time saved (REAL derivation)
**[click]** Return to the dashboard overview.
*"And here's the point for a busy team: what Laura DID, and the hours she gave
back — every number derived from real counts, never fabricated."*
**Expected stat tiles** (keys under `stats` in the `GET /dashboard/summary`
payload): `actions_30d` (captured), `followups_automated_30d`,
`actions_executed_30d` (completed), `avg_readiness_30d`, and
**`hours_saved_30d`** (= captured actions × `roi_minutes_per_action` ÷ 60 — a
conservative, clearly-labelled estimate of manual follow-up hours saved).

---

## The one-line CI guard

The loop above is not just a script — it's a test. This runs it, key-free and
offline, so a broken demo is a red build:

```bash
./scripts/run_demo_e2e.sh
# → backend/tests/test_demo_e2e.py :
#     steps 2-6 end-to-end:  GET /demo/sample → POST /demo/ask (quotes the doc
#       + carries a citation) → POST /demo/post_meeting (summary + actions +
#       risks + readiness + lifecycle fields; owner-gap detection asserted),
#       run twice to prove determinism;
#     step 7 contract:  POST /org/actions/{id}/approve exists and reaches its
#       app-level gate key-free (a rename → red);
#     step 10 contract:  every /dashboard/summary stat tile the tally reads
#       (hours_saved_30d, actions_30d, followups_automated_30d,
#       actions_executed_30d, avg_readiness_30d) is present.
```

## Known key-free gaps (say these if asked)
- **Spoken answers are extractive quotes**, not paraphrase — with a key they read
  conversationally (structure/citation identical).
- **Action capture is heuristic** — some captured rows are question lines, not
  imperatives; a real brain filters to genuine to-dos.
- **`decisions` comes back empty** for the sample transcript in stub mode (the
  offline extractor records no explicit decision); with a key the go-live
  commitment surfaces as one. Lead with summary/risks/actions on camera.
- **Execution is stubbed** — approve records a receipt but performs no external
  write without a connected account. The approve door + stat tiles are contract-
  guarded by the e2e test; the actual approve→execute clicks are presenter-run.
- The **System Check** page is referenced by name (sibling PR); this runbook does
  not depend on its code.
