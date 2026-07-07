# The Wedge — what Laura is, and why it wins

> Single source of truth for **positioning**. If a deck, landing page, agent
> prompt, or README paragraph disagrees with this file, this file is right —
> update the other one. Pair with [`../ARCHITECTURE_CURRENT.md`](../ARCHITECTURE_CURRENT.md)
> (how it actually works).

## One sentence

**Laura is a live AI process agent that makes meetings ready before they start,
complete before they end, and actionable after they finish.**

Not an avatar toy. Not a notetaker. A process agent that happens to have a face.

## The problem

Important recurring meetings (customer onboarding, implementation kickoffs,
decision reviews, steering) have a *process* — required steps, approvals, owners,
decisions. That process lives in people's heads and in docs nobody opens mid-call.
So meetings skip steps: the DPA never got confirmed, no one owns implementation,
"we'll decide next time" three times running. The cost shows up later as a stalled
go-live, a security escalation, or a decision that was never actually made.

Notetakers don't fix this. They summarize *after* — when the gap already happened.

## The wedge

Laura sits **inside** the meeting with the process in hand and closes the loop at
three moments notetakers can't touch:

1. **Before — ready.** Is this meeting (or the next one) set up to succeed? Is the
   objective clear, are the right people here, are the decisions-to-make named, does
   the team have access to start? Laura tracks readiness as it's established.
2. **During — complete.** She silently tracks required process steps, decisions,
   owners, and risks every line — zero added latency (pure regex, no model call).
   She answers grounded, cited questions live. If a **critical** step is still
   missing as the call wraps up, she says so **once** — the intervention that
   prevents the skipped approval.
3. **After — actionable.** The artifact isn't minutes: it's summary + decisions +
   actions + **missing steps** + a **readiness score (0–100)** + a draft follow-up
   email. Execution, not a recording.

The differentiator stack, in order of defensibility:
**process tracking → missing-step prevention → readiness score → follow-up execution.**

## Who it's for

Teams that run the same high-stakes meeting shape over and over and pay when a step
is skipped: customer success / onboarding, solutions & implementation, RevOps,
and any recurring decision/steering forum. Beachhead: **B2B customer onboarding &
implementation**, where a skipped step (security, DPA, access, owner) directly
delays revenue.

## Why not just a notetaker?

| | Notetaker (Otter, Fireflies, Granola, native Zoom/Meet AI) | Laura |
|---|---|---|
| When it acts | after the meeting | before, during, and after |
| What it knows | what was said | what the **process requires** vs. what happened |
| In-meeting value | none (records) | answers live, flags a missing critical step once |
| Output | minutes / summary | readiness score + missing steps + follow-up execution |

## Why now

Meeting-bot infrastructure (Recall.ai) can put a bot with a camera + live
transcript into any Zoom/Meet/Teams call, and fast cheap LLMs (Claude Haiku) make
a grounded, low-latency in-call expert viable. The plumbing is finally bought and
swappable — so the value moves up to the **process/knowledge layer**, which is
where Laura lives.

## Where the moat is (and isn't)

- **Not** the face — the avatar is a commandable mouth; modes swap with one env var.
- **Not** the meeting-bot plumbing — Recall is bought and replaceable.
- **The moat is the process layer**: accurate silent tracking, the template library
  (per-vertical `required_steps` / `critical_gaps`), grounded answer quality, and the
  readiness/artifact that teams start to depend on. Adding a vertical = adding
  templates + a knowledge pack, not rebuilding the product.

## What Laura is deliberately NOT (yet)

- Not a generic Slack/calendar automation bot. Actions (email/Slack/tasks) will come,
  driven by `MeetingState` + the final artifact, provider-independent — **not**
  bolted onto vendor-specific tool calls.
- Not a horizontal "AI copilot." The wedge is vertical process meetings.
- Not an avatar-features product. Face work is done; the roadmap is process depth.

## Proof points today

- Silent `MeetingState` tracker with a **deterministic** closing intervention (no
  model call, no latency) — [`backend/app/meeting_state.py`](../../backend/app/meeting_state.py).
- Four shipped process templates: `customer_onboarding`, `implementation_access`,
  `decision_quality`, `meeting_readiness`.
- Post-meeting artifact with `missing_steps` + `readiness_score`.
- Live in production on AWS App Runner; key-free offline demo for zero-risk trials.
