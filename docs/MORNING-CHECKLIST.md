# Morning checklist — Cedric AI-employee build (night of 2026-07-08 → 09)

All work below is **merged to `main` and deployed to prod**, verified live this
morning (9/9 infra checks + intelligence checks passed). Full test suite: **250
passing**. This is your runbook: what shipped, the ~5 min of human steps only
you can do, and the test script.

> Note: the session restarted mid-build overnight. All **merged** work survived
> (it's on GitHub). One un-pushed piece was lost and rebuilt: the Cedric#4
> connector for Ben's repo (see §5).

---

## 1. What shipped tonight (all live on prod, verified this morning)

| Pack | PR | What | Default | Verified live |
|---|---|---|---|---|
| **Fix Pack** | #61 ✅ | Cedric speaks in **his own voice (Eric)**; answers "can you access Drive?" **correctly**; split-utterance capture | on | ✅ voice=elevenlabs; ✅ "Yes, I have access to the Cedric Test folder" |
| **Artifact hardening** | #63 ✅ | Recap can **never** come out garbled (filters malformed model output) | on | ✅ deployed |
| **Email summon** | #59 ✅ | Invite `laura.ai.122222+cedric@gmail.com` → Cedric joins | on | ✅ |
| **Drive connector** | #60 ✅ | Cedric reads the "Cedric Test" folder at meeting start | on | ✅ end-to-end w/ prod creds |

Earlier tonight: #55 fold-back, #56 render (face+voice), #57 action bridge.

---

## 2. Human steps (only you can do — ~5 min)

### A. Cedric's Slack side (Cedric#4) — for Ben
- I rebuilt the connector as a PR in **SFF-Studio/Cedric** (link in §5). Ben
  reviews/merges, sets 4 env vars in Vercel, points Laura's `callback_url` at
  his receiver. Resume for him: `docs/integration/CEDRIC-4-FOR-BEN.md`.

### B. Optional — prettier email address
- Auto-forward `cedric@<domain>` → `laura.ai.122222+cedric@gmail.com`, tell me
  the address, I add it to `CALENDAR_INVITE_EMAILS`.

---

## 3. The 5-minute live meeting test
1. [meet.new](https://meet.new) → **Add people** → invite
   `laura.ai.122222+cedric@gmail.com`. Admit "Cedric".
2. **Voice + face**: tile *Cedric*, male face, **Eric's voice** (the bug you found).
3. **Drive grounding**: *"Cedric, what's the status of Project Apollo?"* → green,
   pilot July 15.
4. **Self-knowledge**: *"Cedric, can you access our Google Drive?"* → **yes**
   (already confirmed live this morning).
5. **Action capture**: *"Cedric, send the recap to the team after the call."* →
   "Got it — I'll queue that for approval in Slack right after the call."
6. **Counter-test** (must ANSWER, not queue): *"Cedric, can you check if the
   numbers add up?"*
7. End: *"Cedric, you can leave."* Then send me the meeting link — I'll pull the
   artifact (summary, decisions, recap action `requested_live: true`, readiness,
   meter stopped) and confirm the `session.ended` hand-off to Cedric fired.

---

## 4. Honestly not yet verified (needs you / a real meeting)
- **Audio** of Eric's voice — code path + prod synthesis confirmed (elevenlabs,
  282ms); step 2 is the ear check.
- **Ben's Slack execution** — his PR awaits review.
- **Calendar-event auto-booking** — now Cedric's job (roadmap NOW #1); Laura
  hands the dated actions across, he books the holds with his own tools.

## 5. Links
- Commercial roadmap (what to build next, prioritized): `docs/COMMERCIAL-ROADMAP.md`
- Cedric#4 PR (Ben's side): _see SFF-Studio/Cedric PR list — rebuilt this morning_
- Surface API contract: `docs/integration/SURFACE-API.md`
- Storage durability decision (org memory survives deploys): PR #58 — **merge this**
