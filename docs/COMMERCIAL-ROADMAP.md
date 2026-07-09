# Cedric / Laura — commercial roadmap for the "AI employee"

Grounded in what the code does **today** (2026-07-09) and where a company buyer's
value actually sits. Positioning vs. the notetaker crowd (Otter, Fireflies,
Fathom, Read.ai, Circleback, Granola, tl;dv): those *transcribe and summarize*
after the fact. Cedric is **present, grounded, and acts** — he answers from your
docs live, captures asks in the meeting, and does the follow-up work himself.
That "does the work" wedge is the moat; the roadmap deepens it.

## Already shipped (the baseline to sell)
- Joins Zoom/Meet/Teams as a named avatar, own face + voice, wake word.
- Answers **grounded** live: RAG over process docs + the shared Drive folder +
  web search for current facts.
- Walks in **briefed**: pre-meeting brief from a Drive folder + cross-meeting
  ledger ("what's still open from last time").
- **Captures action requests live** → signed webhook → Slack approval card.
- Post-meeting **artifact**: summary, decisions, actions w/ owners + deadlines,
  risks, readiness score, per-person participation.
- **Does the work itself**: the agreed actions (recap email, notes, follow-ups)
  are handed to Cedric, who executes them with his own Slack tools.
- Multi-avatar platform (folder = new agent), each with own email
  (`+alias`), voice, face, Drive folder, knowledge.
- Org-memory API + surface contract (Cedric-in-Slack is client #1).

## NOW — the shortest path to "a company would pay for this"
Each is small, self-contained, and rides seams that already exist.

1. **Calendar follow-ups (now Cedric's job)** — booking a follow-up hold on the
   connected calendar for actions that carry a due date is **owned by Cedric**,
   who holds the calendar tools. Laura's part is to hand the dated actions across
   cleanly in `session.ended`; the calendar wiring lives on Cedric's side. Value:
   the "I'll set that up" promise becomes real without Slack.
2. **Ask-across-meetings endpoint** — `GET /org/search?q=` over the ledger +
   stored artifacts ("what did we decide about pricing last month?"). The data
   exists (ledger + artifacts store); this is a query layer + a small UI card.
   *Effort: ~1 day.* Value: the "employee that remembers" story, demoable.
3. **CRM push (HubSpot/Salesforce)** for the sales ICP — at finalize, append
   the recap + action items to the matched deal/contact. Lands on Cedric's
   execution side, following his best-effort, gated, off-path connector pattern.
   *Effort: ~1–2 days.* Value: this is what sales teams actually buy.
4. **Storage durability** (PR #58 pending) — org memory currently dies on every
   deploy (sqlite on ephemeral disk). Fatal for the "remembers" pitch. Ship the
   Litestream→S3 sidecar. *Effort: ~half day + a decision.* **Do before #2/#3
   are load-bearing.**

## NEXT — differentiation & stickiness
5. **Task-tool push** (Linear / Asana / Notion / Jira) — actions become tickets.
   Same connector pattern; one per integration.
6. **Meeting-type templates as a product surface** — the `process_templates/`
   engine (readiness scoring, missing-step detection) already exists; expose
   template selection + a "meeting readiness" pre-call check to the buyer.
7. **Live coaching nudges** — the quiet-participant nudge + proactive
   missing-step intervention already ship; add agenda-timeboxing ("we're 10 min
   over on item 2") and decision-forcing prompts. This is the "Read.ai but it
   talks" angle.
8. **Recording + searchable archive with speaker labels** — `/meetings` exists;
   add diarization (pyannote stage 0 is in-repo) + full-text search + share
   links. Table-stakes vs. notetakers, and the archive is where org memory is
   visible.
9. **Real approval buttons in Slack** (Cedric side) — today approval = typing
   "approve"; wire the Block Kit buttons for a one-tap execute.

## LATER — platform / moat
10. **Per-client key registry** (multi-tenant) — the Surface API is built for it
    (single shared token today); needed before a 2nd external customer.
11. **Vertical avatars** — recruiting-agency avatar (#53 spec) as the first
    packaged vertical; the folder-per-agent model makes this a content play, not
    an eng play.
12. **Analytics dashboard** — talk-share, decisions/meeting, action-completion
    rate (ledger resolve data), meeting-readiness trend. Sells to managers.
13. **Compliance** — recording-consent disclosure line at join, data-retention
    controls, per-org PII policy. Required for regulated buyers.

## Sequencing recommendation
Ship **NOW #4 (durability) → #2 (ask-across-meetings) → #1 (calendar) → #3
(CRM)** in that order: durability unblocks the memory story, ask-across-meetings
makes it demoable, calendar + CRM make it *do the sales-team's work*. That
sequence is the fastest route to a paid pilot.
