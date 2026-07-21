# Laura: First Cold-Outreach Experiment

> **Purpose:** decide whether there's real, reachable market interest before spending
> more on GTM. This is an experiment *design*, not copy. Copy/sequencing execution is
> **growth-gtm**'s job. Prospecting/enrichment already done; inventory is ~32 enriched
> B2B leads with verified emails across two niches.
> **Owner:** founder · **Status:** proposed · **Last reviewed:** 2026-07-09

---

## TL;DR: recommendation

**Run Niche 1 (SaaS) first, single pitch angle, all 17 leads as one probe.** It is the
only niche where Laura's *differentiator* works end-to-end today: her two live-shipping
templates (`avatars/laura/process_templates/customer_onboarding.yaml`,
`implementation_access.yaml`) describe a SaaS onboarding / implementation-kickoff call
line-for-line, and the silent tracker that powers the missing-step warning + readiness
score is **English-only regex** (`backend/app/meeting_state.py`). English SaaS calls ->
the whole wedge fires. Italian HR calls -> only the conversational brain fires, and Laura
degrades to a notetaker. HR-Italy (Niche 4) is a sharper vertical wedge and likely
less-crowded, but it is a **second**, gated on either a signal from Batch 1 or a small
Italian-tracking build. Tradeoff stated honestly in §2.

---

## 0. What this experiment is (and is NOT)

This is a **signal-quality probe**, not a statistically significant test. With ~15–17
leads per niche we can learn *whether buyers in this ICP recognize the pain in our own
words* and *what they push back on*: we **cannot** learn a reliable reply rate or
conversion %. Every threshold below is built around **existence of genuine engagement +
the content of replies**, never a percentage. Read §3 before trusting any number.

---

## 1. The two hypotheses: one falsifiable claim per batch

| Batch | Buyer (who must feel it) | Pain being tested | Falsifiable hypothesis | Primary kill signal |
|---|---|---|---|---|
| **B1: SaaS (17)** | Head of CS / CSM / Implementation Manager / COO at EU-or-remote SaaS | Onboarding / implementation kickoff calls skip a step (security review, DPA, access, technical owner) and go-live stalls -> delayed revenue | *These buyers feel the dropped-step onboarding stall acutely enough that a live in-meeting agent which flags the missing step earns a reply or a look.* | Silence + zero genuine positive replies across both angles (see §4) |
| **B4: HR-Italy (15)** | Founder / principal / recruiting manager at Italian boutique exec-search firm | Interview debriefs & candidate/client coordination calls lose the decision (advance/reject), the owner, and the next step -> slower time-to-hire, inconsistent candidate experience | *Italian boutique-search principals feel the lost-decision pain enough to reply; even in Italian, even before Laura tracks Italian calls.* | 0 genuine positive replies across 15 (weaker read; product is weaker here; see §2) |

Each hypothesis names **one buyer** and **one pain**. If the reply data contradicts the
buyer (wrong title answers) or the pain (they argue a different problem), the hypothesis
is falsified in a legible way, not just "no."

---

## 2. Which niche first: and the honest tradeoff

**Recommendation: Niche 1 (SaaS), first and alone.**

**Why, grounded in what the code does TODAY:**
- Laura ships four process templates; two of them, `customer_onboarding` (security
  approval, DPA, implementation owner, customer handoff, go-live date) and
  `implementation_access` (access granted, technical owner, environment ready,
  integration scope, kickoff scheduled): *are* the SaaS onboarding-call checklist. The
  demo you'd show is the exact artifact this buyer wants.
- The silent tracker, closing intervention, and readiness score in
  `backend/app/meeting_state.py` are **pure English regex** (`_TYPE_HINTS`, `_DONE`,
  `_DECISION`, `_OWNER_PATTERNS`). On an English SaaS call the whole "not a notetaker"
  wedge fires. This buyer, this language, this pain = the tightest product-match we have.
- English outreach + English async demo (key-free offline demo / Loom of a real onboarding
  call -> artifact). Zero-key guarantee makes "run it yourself, no signup" a valid ask.

**The honest tradeoff; why NOT HR-Italy first (even though it's a sharper wedge):**
- **The moat feature is blind in Italian.** MeetingState's detection regex is English-only.
  An Italian debrief transcript won't trip `_DECISION` / `_DONE` / the type hints, so the
  missing-step warning + readiness score won't fire. Laura's *brain* speaks Italian
  (LLM), so she can still answer live and produce an artifact; but that's a **conversational
  notetaker**, i.e. the exact thing WEDGE.md says we are NOT. Demoing a degraded product to
  the sharpest vertical is the wrong first impression.
- Making HR-Italy a real product-match is genuine work, not a folder drop: **Italian
  detection regex in `meeting_state.py` + an Italian interview-debrief template in
  `avatars/<id>/process_templates/` + an Italian knowledge pack** in `avatars/<id>/knowledge/`.
- Different buyer psychology (owner-operator principal vs. a CS function lead) and
  **Italian-language copy**: a second variable stacked on an unproven message.
- **Upside acknowledged:** boutique exec search is high call-volume, tightly defined, and
  less crowded than SaaS-CS tooling. It is the **highest-potential second**, not a discard.

**One-line tradeoff:** *SaaS = closest product-match, everything works today, more
competition; HR-Italy = sharper wedge, less competition, but Laura can't yet deliver her
core value in Italian.* Start where the product is real.

---

## 3. What counts as "interest": the signal ladder

| Strength | Signal | Weight for a 15-lead batch |
|---|---|---|
| Vanity | open / click | **Ignore** for a batch this small (noise + tracker inaccuracy). Use only as a *deliverability* sanity check; did anything land at all. |
| Weak-positive | a **human reply that engages the pain**: incl. "interesting but not now", "who else uses this?", "we already use X" | **The primary signal.** A real human argued with our words. |
| Medium | asked to see it / watched the async demo to the end / "send more" | Real intent; a buyer spent time. |
| Strong | **booked a 15-min call** | The signal we're actually hunting. |

**What ~15–17 leads/niche CAN tell us:** *signal quality.* Do buyers in this ICP
recognize the pain in our framing? Do they argue the pain, the price, or "we already have
a notetaker"? Is the vertical legible enough to write to? Is even one buyer a yes
(existence proof)?

**What it CANNOT tell us:** *reply rate or conversion.* n=15 has a huge confidence
interval: 1/15 vs 2/15 is noise. **Do not compute a "reply %."** Do not conclude "SaaS
converts at 12%." A single genuine positive is meaningful as *existence proof*; a zero is
**not** proof of no-market (could be copy, timing, deliverability, or wrong contact).
Decisions lean on **content + any genuine engagement**, plus a deliverability gut-check.

---

## 4. Thresholds: success / iterate / kill

Inventory reality: **Niche 1 = 17 leads, Niche 4 = 15 leads.** 17 is too few to A/B two
angles inside (8 vs 9 learns nothing and burns the list). So: **one strong angle per
batch**; test a second angle only on a *fresh* follow-up batch.

**Batch 1, SaaS, all 17, Angle A** ("missing-step / stalled go-live", the closest match
to `customer_onboarding` critical gaps):

| Outcome | Trigger | Action |
|---|---|---|
| **Success: double down** | **≥2 genuine positive replies OR ≥1 booked meeting** in 17 | Message + ICP resonate. Recruit the warmest reply as a **design partner** (ingest their real onboarding docs -> the knowledge moat), send a second fresh SaaS batch on Angle A, and prep a light deck. |
| **Iterate: message check** | Exactly 1 positive, OR several "engaged-but-no / wrong person" replies, OR mostly silence with clean deliverability | Borderline. Pull a **fresh ~15 SaaS batch** and run **Angle B** ("readiness score / early-warning before churn") to isolate message vs. angle. (Needs fresh Clay leads; flag to prospecting.) |
| **Kill / re-ICP** | **0 genuine positives across BOTH angles** (Angle A on 17 + Angle B on a fresh ~15 ≈ 32 SaaS touches) | Message *or* ICP is wrong. **First fix ICP granularity** (e.g. Implementation Managers at Series A/B SaaS with an enterprise onboarding motion) before abandoning the niche. If a narrowed 20–30 still returns zero -> pause SaaS cold outreach; move down the niche sequence (§6) or switch to warm channels/communities. |

**Batch 4: HR-Italy, all 15, single Italian angle** (only if triggered per §6):

| Outcome | Trigger | Action |
|---|---|---|
| Worth a deeper look | **≥1 genuine positive OR 1 meeting** in 15 | Existence proof the vertical is reachable. Decide whether to fund the Italian-tracking build (§2) for a real product-match. |
| Inconclusive | 0 positives across 15 | **Do not over-read.** Could be Italian copy, the degraded (notetaker-only) product, or reachability. Don't kill the vertical on 15; revisit after the Italian-tracking build or with a warmer channel. |

---

## 5. Reply content -> product & positioning feedback loop

The replies are the real payload. Route each objection to the right fix. **most are copy,
not code.**

| What they say | What it means | What changes (and where) |
|---|---|---|
| "We already use Otter / Fireflies / Granola." | Positioning gap, not product gap | **Copy only.** Sharpen the "not a notetaker; acts *during* the call, flags the missing step" wedge (already in `docs/product/WEDGE.md`). No roadmap change. |
| "Our onboarding process isn't written down." | **Product gap**: Laura needs the process doc to be useful (CONTEXT: "real usefulness gated on ingesting real process docs") | **Roadmap.** A low-friction "we build your template/knowledge pack from one recorded kickoff or a 30-min interview" onboarding-of-the-process. Confirms the ingestion moat (`backend/scripts/ingest.py`, `avatars/<id>/knowledge/`) is the true bottleneck. |
| "Does it work on Teams / our stack?" | Confidence in plumbing | **No change**: Recall covers Zoom/Meet/Teams. Answer in copy. |
| "A bot in our customer calls: security/PII?" | Trust/compliance need surfaced | **Packaging, not code.** A "how Laura handles transcripts (memory-only, never logged)" one-pager. We already satisfy this (hard constraint 6). |
| "Can it push actions to HubSpot / Slack?" | Validates the deferred-actions bet | If **repeated**, raise priority of the provider-independent actions item flagged in WEDGE.md ("actions will come… not bolted onto vendor tool calls"). |
| Replies latch onto the **readiness score** (not missing-step) | The hook is the score, not the warning | Swap the **default pitch angle** to readiness/early-warning; feed to growth-gtm. |
| Replies latch onto the **follow-up email / artifact** | The hook is post-meeting execution | Lead copy with the artifact, not the live intervention. |
| (Niche 4) "It should understand our Italian calls." | Confirms the Italian-tracking gap is the blocker | **Roadmap.** Prioritize Italian regex + Italian template + Italian knowledge (§2) before scaling HR-Italy. |

**Rule:** a copy/positioning objection = hand to growth-gtm; a "Laura can't do X" objection
that repeats = a roadmap candidate. Log every reply verbatim; the pattern across replies is
the deliverable, not any single one.

---

## 6. Sequencing across all 4 niches

One niche at a time. **never spray all 32 leads in parallel.** You can't learn from a blur;
each niche gets the current best-known pitch, and you only advance when a message pulls (or a
clean kill).

- **NOW: Niche 1 (SaaS, 17).** Closest product-match; English; MeetingState fires; key-free
  demo. Angle A first; Angle B only on a fresh batch per §4.
- **NEXT: Niche 4 (HR-Italy, 15).** Highest-upside second (sharp vertical, high call volume,
  less crowded). Gate it: run **only if** (a) Batch 1 gives a usable message read **and** (b)
  we either fund the minimal Italian-tracking build (§2) *or* consciously run it as a weaker
  "conversational Q&A + artifact" pitch and label it as testing a degraded product. Prefer the
  build first; don't demo a notetaker to the sharpest wedge.
- **LATER: Niche 2 (consulting / digital transformation), then Niche 3 (agencies).** Both map
  to `decision_quality` / `meeting_readiness` (steering calls, client status/approval), English,
  so the product works; but the pain is more diffuse ("decisions get lost") and the buyer more
  varied. Run only after Niche 1 (and/or 4) establishes a pitch that pulls, reusing the winning
  angle. **Niche 2 before Niche 3**: consulting steering calls fit `decision_quality` more
  tightly than agency status calls.

**Guardrail:** advancing to the next niche requires *either* a working pitch from the prior
niche *or* an explicit decision to explore, with fresh leads; not recycling the same 32.

---

## Constraints honored

No capabilities invented; every claim maps to shipped code (`meeting_state.py`, the four
`process_templates/*.yaml`, the offline key-free demo). This doc designs the test only; **it
sends nothing and drafts no copy** (growth-gtm owns copy + send). Zero-key demo guarantee and
vendor-swappability are untouched.
