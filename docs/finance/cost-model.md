# Laura — Cost Model (per meeting-minute + monthly)

**Prepared:** 2026-07-10. Pricing verified via web search on this date unless
marked **[ASSUMPTION]** or **[UNVERIFIED]**. Prices move — re-check before a
board deck or a customer contract cites a number from here.

---

## Headline

**Removing Anam takes a 30-minute meeting from ~$4.00 to ~$0.40** (open-source
`/talk` face, production LLM mix). Depending on two unresolved verification
gaps (Recall's Output Media surcharge, exact eu-central-1 App Runner rate) the
honest range is **$0.35–$0.55**. Either way, Recall is now **>75% of the
bill** — the avatar face used to dominate (Anam ~90%); now the meeting-bot
vendor does.

---

## 0. What LLM is *actually* live in production (verified 2026-07-10)

**Correction to an earlier version of this section.** A prior draft claimed prod
ran Claude Haiku live and dismissed Cerebras as "not implemented / aspirational."
That was wrong — it read the *code defaults* and a stale `ARCHITECTURE_CURRENT.md`
but never checked the running App Runner config. Verified directly against the
live service (`aws apprunner describe-service` on `laura-backend`, 2026-07-10):

- **Live spoken path = Cerebras `gemma-4-31b`.** Prod env: `BRAIN_PROVIDER=groq`
  **but** `GROQ_BASE=https://api.cerebras.ai/v1` with the Cerebras API key stored
  under `GROQ_API_KEY` (SSM `/laura/prod/CEREBRAS_API_KEY`). The `groq` provider is
  a generic OpenAI-compatible client, so this tunnelled every live answer to
  Cerebras — **consuming Cerebras credits on every meeting turn.**
- **Post-meeting artifact = Claude Sonnet 5** (`BRAIN_PROVIDER_POST=anthropic`,
  `BRAIN_MODEL=claude-sonnet-5`). This part was always correct.
- **Claude Haiku** is the complex-reasoning tier, the web-search model, and the
  circuit-breaker fallback if Cerebras rate-limits — not the primary fast path.

**Cleanup shipped 2026-07-10:** Cerebras is now a **first-class provider**
(`BRAIN_PROVIDER=cerebras`, `CEREBRAS_API_KEY`, `CEREBRAS_BASE` — see
`backend/app/config.py` / `llm.py`), so the code names match reality instead of
hiding Cerebras behind `GROQ_*`. The one doc the earlier draft called "stale"
(`avatars/laura/about/provider_cost_and_replacement.md`) was in fact **correct**.

**Punchline (unchanged):** at this call volume the LLM choice moves the 30-min
total by low single-digit cents — **it is not a meaningful cost lever** (it *is*
a latency lever, which is why prod pays for Cerebras). Recall and (if enabled)
the photoreal GPU are the real cost drivers. The Section 3 rows previously labelled
"hypothetical Cerebras" are, in fact, the **live** configuration.

---

## 1. Assumptions (stated explicitly — nothing below is silent)

| Assumption | Value | Basis |
|---|---|---|
| Average call length | 30 min | as requested; per-minute/hour also shown |
| Live grounded answers per 30-min call | 8 (~1 every 4 min) | **[ASSUMPTION]** — reasonable for a conversational process-Q&A avatar; not measured in prod |
| Tokens per live answer | 1,200 in / 150 out | **[ASSUMPTION]**, grounded in code: `brain.py` caps live answers at `max_tokens=400`; 150 is a typical spoken 2–3 sentence answer, well under the cap |
| Post-meeting artifact generations | 1 per call | product spec — always runs at session end |
| Tokens per artifact | 7,000 in / 1,500 out | **[ASSUMPTION]** — full 30-min transcript (~4–6k tokens) + process templates + system prompt (in); multi-field JSON (summary/decisions/actions/risks/missing_steps/readiness/follow-up email) capped at `max_tokens=4000` in code, 1,500 is a realistic fraction |
| Meetings/seat/month by tier | Free 3 (90 min) · Pro 20 (10 hr) · Business 30 (15 hr) · Enterprise 200 (100 hr) | Free/Pro/Business hours as given in the brief; **Enterprise hours are an unstated assumption** — no public number exists, flagged |
| Prices | Free $0 · Pro $59/user · Business $149/seat · Enterprise ~$5k+/mo | as given in the brief |

---

## 2. Vendor-by-vendor pricing (verified 2026-07-10)

### 2.1 Recall.ai — ears + camera

- **Bot recording (Pay-As-You-Go):** **$0.50/hour**, down from $0.70/hr per
  Recall's own 2026 pricing post. [Recall 2026 pricing blog](https://www.recall.ai/blog/new-recall-ai-pricing-for-2026)
- **Built-in transcription:** **$0.15/hour**. [Recall pricing](https://www.recall.ai/pricing), [Recall usage docs](https://docs.recall.ai/docs/calculating-usage)
- **Output Media** (streaming our avatar page as the bot's camera — what
  Laura actually uses, not plain recording): Recall's own docs describe the
  feature ([docs.recall.ai/docs/stream-media](https://docs.recall.ai/docs/stream-media))
  but **no separate published per-hour rate was found** distinct from the
  base bot-hour rate above. **[UNVERIFIED — flag before scaling]**: this
  model assumes Output Media bills at the same $0.50/hr bot rate. If Recall
  charges a real-time-streaming premium (plausible; not disclosed publicly),
  budget +50–100% on the Recall line ($0.49–$0.65/30-min instead of $0.325).
  **Action: confirm directly with Recall before a volume commitment.**
- **Combined estimate: $0.65/hr = $0.325 / 30-min meeting.**
- Storage beyond 7 days: $0.05/media-hour per extra 30 days (not modeled —
  transcripts are kept in-memory + artifact only per the PII constraint, no
  long-term Recall storage is used).

### 2.2 LLM — four ways to price the same slot

| Engine | Status in Laura | $/M in | $/M out | Source |
|---|---|---|---|---|
| **Claude Haiku 4.5** | **PROD** — complex-reasoning tier + web-search model + breaker fallback (NOT the primary live path) | $1.00 | $5.00 | verified [multiple 2026 pricing trackers](https://www.metacto.com/blogs/anthropic-api-pricing-a-full-breakdown-of-costs-and-integration) |
| **Claude Sonnet 5** | **ACTUAL PROD default (post-meeting artifact)** | $2.00 (intro, through 2026-08-31) → $3.00 after | $10.00 (intro) → $15.00 after | verified [TLDL Anthropic pricing](https://www.tldl.io/resources/anthropic-api-pricing), [pricepertoken](https://pricepertoken.com/pricing-page/model/anthropic-claude-sonnet-4.5) |
| **Groq** `llama-3.3-70b-versatile` | Wired in `backend/app/llm.py`, **off by default in prod** (optional fast path + circuit-breaker fallback) | $0.59 | $0.79 | verified [Groq pricing](https://groq.com/pricing) |
| **Cerebras** `gemma-4-31b` | **ACTUAL PROD live path** — first-class provider (`BRAIN_PROVIDER=cerebras`) as of 2026-07-10; previously tunnelled via `GROQ_BASE`. Consumes Cerebras credits per live answer | $0.85 [UNVERIFIED — see note] | $1.20 [UNVERIFIED] | third-party trackers only; [Cerebras pricing page](https://www.cerebras.ai/pricing) lists this model as **dedicated-endpoint/custom pricing, not on the public self-serve rate card**. Cerebras' actual self-serve public model today is **GPT-OSS-120B at $0.35/$0.75 per M** (verified) |
| Ollama (`llama3.2`, local) / stub | Dev/offline only | $0 | $0 | n/a — zero-key demo requirement |

Per-30-min-call LLM cost (assumptions from §1: 8 live answers × 1,200in/150out
+ 1 artifact × 7,000in/1,500out):

| Scenario | Live cost | Artifact cost | **Total LLM / 30-min call** |
|---|---|---|---|
| **Actual production** (Haiku live + Sonnet post) | $0.016 | $0.029–$0.044 | **$0.045–$0.060** |
| Hypothetical Cerebras live + Sonnet post | $0.010 | $0.029–$0.044 | $0.039–$0.054 |
| Groq live + Sonnet post | $0.007 | $0.029–$0.044 | $0.036–$0.051 |
| Cheapest possible (Cerebras or Groq for *both* live+artifact, no Sonnet quality) | $0.007–$0.010 | $0.006–$0.008 | **$0.013–$0.018** |

**Takeaway: swapping the live engine saves ~1 cent per 30-min call. Swapping
the artifact engine off Sonnet saves ~3 cents. Neither moves the total call
cost meaningfully** — Recall (~$0.325) is 6–20× any of these LLM deltas.
Cerebras' real selling point isn't cost here, it's **latency** (170ms
time-to-first-token vs Haiku's ~600–750ms) — see §6 for the speed comparison.

### 2.3 Face + voice

- **`/talk` (open-source, prod default):** $0 marginal per meeting.
  TalkingHead WebGL + vendored `laura.glb`, voice via free `edge-tts` fallback
  or ElevenLabs `eleven_flash_v2_5` under a flat monthly plan (see §4 fixed
  costs). A 30-min call's spoken output at 8 answers × ~700 characters ≈
  5,600 characters — a rounding error against any ElevenLabs plan.
- **`/avatar` (Anam, legacy fallback only):** **$0.12/min** verified 2026-07-10
  ([anam.ai/pricing](https://anam.ai/pricing)) → **$3.60 / 30-min call** for
  face+voice alone. This is the number the "~$4/call" legacy figure is built
  on, and it is **not the default path** — `/talk` is.

### 2.4 AWS App Runner backend (eu-central-1, 1 vCPU / 2 GB)

Verified rate card (App Runner official pricing page — quoted for
US East/Ireland; **eu-central-1/Frankfurt is not separately broken out in the
sources found — [ASSUMPTION] that Frankfurt sits in the same standard tier**,
confirm on calculator.aws before relying on this at scale):
[AWS App Runner pricing](https://aws.amazon.com/apprunner/pricing/)

- **Memory (always billed, idle or active): $0.007/GB-hour.**
- **Compute (billed only while the container is "active"/processing a
  request): $0.064/vCPU-hour**, on top of the memory rate.

Laura's backend is a persistent process (open WebSocket to the avatar page,
a 15-second Gmail-poll loop, in-memory session state) — it is realistically
"active" for a meaningful fraction of the day even between meetings, which is
consistent with the ~$15/mo the team already observes on the actual bill
(cited in `.claude/CONTEXT.md`/`README.md`):

| Component | Calc | Monthly |
|---|---|---|
| Memory floor (2 GB × $0.007 × 730 hr, paid continuously) | | **$10.22** |
| Active-vCPU time (webhooks, poll loop, live meetings — implied by the observed ~$15/mo bill) | ~75 active-vCPU-hr/mo × $0.064 | **~$4.78** |
| **Total fixed baseline** | | **~$15/mo** |

- **Marginal cost of one more 30-min meeting** (the number that belongs in the
  per-meeting table): 0.5 hr × $0.064/vCPU-hr = **$0.032**. Memory is already
  covered by the always-on floor, so no extra memory charge per meeting.
- **Fully-loaded allocation** of the $15/mo fixed baseline over assumed
  volume: at 100 half-hour meetings/month → **$0.15/meeting**; at 500/month
  → **$0.03/meeting**. State your volume before quoting this line — it swings
  5×.

### 2.5 Photoreal GPU (Stage 2, optional add-on) — the corrected number

**Old assumption (in `gpu/README.md` / `.claude/CONTEXT.md` today):** AWS EC2
`g5.xlarge` on-demand, eu-central-1, **$1.006/hr → $0.503/30-min**.

**Corrected:** a cheap community-cloud GPU is enough for real-time MuseTalk
inference (modest VRAM/compute footprint — a few GB active, well within a
24 GB card):

| SKU | Provider | $/hr | $/30-min meeting | Source |
|---|---|---|---|---|
| **L4 24GB (primary recommendation)** | RunPod Community Cloud | **$0.39** | **$0.195** | verified July 2026, [gpuvec.com RunPod tracker](https://gpuvec.com/providers/runpod) |
| RTX 4090 24GB (cheaper, consumer-grade) | RunPod Community Cloud | $0.34 | $0.17 | [Northflank RunPod breakdown](https://northflank.com/blog/runpod-gpu-pricing) |
| *(old assumption, for comparison)* g5.xlarge (A10G) | AWS EC2 on-demand | $1.006 | $0.503 | `gpu/README.md` (superseded by this doc) |

**New target: ~$0.15–$0.24 / 30-min meeting** (L4 as the primary pick,
$0.195 typical), a **~2.5× reduction** from the old AWS EC2 assumption, and
solidly inside the "clearly under $0.50/30min" bar. Community-cloud pricing
is a spot/marketplace rate — it floats with demand and can be preempted
mid-session (a reliability tradeoff vs. the previous dedicated EC2 box); the
four independent auto-stop layers in `gpu/README.md` still apply and prevent
idle billing regardless of which SKU runs the box.

---

## 3. Cost per 30-minute meeting — full tables

### 3a. `/talk` path (open-source face, prod default) — actual production LLM mix

| Line | Cost |
|---|---|
| Recall (recording $0.25 + transcription $0.075) | $0.325 |
| LLM — live (Claude Haiku, 8 answers) | $0.016 |
| LLM — post-meeting artifact (Claude Sonnet) | $0.029–$0.044 |
| Face/voice (`/talk`, open-source) | $0.00 |
| AWS App Runner (marginal active compute) | $0.032 |
| **Variable subtotal** | **$0.40–$0.42** |

### 3b. `/talk` path — Cerebras-live (the ACTUAL prod configuration)

| Line | Cost |
|---|---|
| Recall | $0.325 |
| LLM — live (Cerebras `gemma-4-31b`, shipped) | $0.010 |
| LLM — post-meeting artifact (Sonnet) | $0.029–$0.044 |
| Face/voice | $0.00 |
| AWS backend (marginal) | $0.032 |
| **Variable subtotal** | **$0.40–$0.41** — essentially identical to 3a; confirms the LLM engine isn't the lever |

### 3c. Legacy `/avatar` path (Anam face) — for the headline comparison

| Line | Cost |
|---|---|
| Recall | $0.325 |
| LLM (Haiku + Sonnet) | $0.045–$0.060 |
| Face+voice (Anam, $0.12/min) | $3.60 |
| AWS backend (marginal) | $0.032 |
| **Total** | **~$4.00** |

### 3d. Add-ons (apply on top of 3a)

| Add-on | Cost/30-min | Note |
|---|---|---|
| + Photoreal GPU (RunPod L4 community cloud) | +$0.15–$0.24 | optional Stage-2 visual upgrade; corrected from the old $0.50 assumption |
| Sonnet-quality artifact vs. cheapest-LLM artifact | +$0.02–$0.03 | already included in 3a; shown here as the delta if you downgraded the artifact to Cerebras/Groq instead |

**Full stack (open-source face + photoreal GPU + Sonnet artifact): ~$0.55–$0.65 / 30-min meeting.**

### Headline, precisely stated

**~$4.00 (legacy Anam) → ~$0.40 (open-source `/talk`, actual prod LLM mix),
range $0.35–$0.55** depending on the two open verification items (Recall
Output Media surcharge, exact Frankfurt App Runner rate). Recall is now
**75–85% of the bill**; the LLM is 5–15%; AWS backend is ~8%. The old
dominant cost (Anam, ~90% of the bill) is gone from the default path.

---

## 4. Per-minute / per-hour blended (Path 3a, no add-ons)

| Unit | Cost |
|---|---|
| Per meeting-minute | **~$0.014** ($0.41 / 30) |
| Per meeting-hour | **~$0.82** |
| Per meeting-minute, + photoreal GPU | ~$0.020 |
| Per meeting-hour, + photoreal GPU | ~$1.22 |

---

## 5. Fixed monthly costs

| Item | Cost/mo | Note |
|---|---|---|
| AWS App Runner baseline (1 vCPU/2GB, eu-central-1) | **~$15** | memory floor $10.22 + observed active-compute time; see §2.4 |
| ElevenLabs voice plan (optional — only if not using free `edge-tts`) | **$22** (Creator plan, 121,000 credits) | verified 2026-07-10; ~40+ half-hour calls/month of speaking before overage at current per-call character usage |
| WorkOS (auth) | **$0** until the first enterprise SSO/SCIM connection; then **$125/mo per connection** (volume discounts at scale) | verified 2026-07-10 |
| **Total fixed baseline (pre-SSO, using ElevenLabs)** | **~$37/mo** | |
| **Total fixed baseline (free `edge-tts`, no SSO yet)** | **~$15/mo** | |

These are trivial once amortized over any real seat count (e.g., $37/mo ÷ 20
Pro seats = ~$1.85/seat/mo) — they do not change the gross-margin picture in
§6, they just need to exist in the model.

---

## 6. Margin per pricing tier (ballpark, gross margin only — vendor costs, not CAC/support/eng)

Using the blended **$0.0137/min** variable rate from §4 (Path 3a, no add-ons;
photoreal is a premium add-on, not assumed here) and the meeting-volume
assumptions in §1:

| Tier | Price | Included usage | Variable cost | Contribution margin | Margin % |
|---|---|---|---|---|---|
| Free | $0 | 90 min/mo | $1.23/user/mo | **–$1.23/user/mo** | n/a (acquisition cost) |
| Pro | $59/user/mo | 10 hr (600 min) | $8.22/seat/mo | **$50.78/seat/mo** | **86%** |
| Business | $149/seat/mo | 15 hr (900 min) | $12.33/seat/mo | **$136.67/seat/mo** | **92%** |
| Enterprise | ~$5,000+/mo | **[ASSUMPTION: 100 hr/mo pooled — no public number]** | $82.20/mo | **$4,917.80/mo** | **~98%** |

- Free tier: a real per-user cost, not free to serve — budget it as CAC, not
  as a rounding error.
- Pro/Business margins are **healthy and dominated by Recall**, not by the
  brain or the face — the old model (Anam-era) would have shown Pro at
  **negative** contribution margin (20 hrs × $0.12/min Anam alone = $144 >
  $59 price). That was the real problem the Anam removal fixed.
- Enterprise margin is meaningless without the real included-usage number in
  the contract — flag this before using the 98% figure anywhere external.
- None of the above includes the ~$15–37/mo fixed baseline (immaterial once
  amortized) or engineering/support/success cost (out of scope for a unit-
  economics model, material for a full P&L).

---

## 7. Quick technical comparison — "which engine where" (Cerebras vs Groq vs Sonnet vs Ollama/stub)

| Engine | First-token latency | Throughput | $/M in | $/M out | Quality | Best use | Status in Laura |
|---|---|---|---|---|---|---|---|
| **Cerebras** (Llama-3.3-70B) | **~170ms** ([Artificial Analysis](https://artificialanalysis.ai/models/llama-3-3-instruct-70b/providers)) | **~2,300–2,500 tok/s** | $0.85 [unverified, custom pricing] | $1.20 [unverified] | Same weights as Groq's model — good, not top-tier; weaker JSON discipline than Claude | **PROD live gate + answer** (fastest) | **Live in prod** — first-class `BRAIN_PROVIDER=cerebras` |
| **Groq** (`llama-3.3-70b-versatile`) | ~0.95–1.0s (measured on this system, `docs/research/llm-options-for-live-laura.md`) | ~250–330 tok/s | $0.59 | $0.79 | Good; weaker JSON discipline; free tier 429s under load (circuit breaker in `llm.py` routes around it) | Live gate + answer, cheap alt | Wired in code, **off by default in prod** |
| **Claude Haiku 4.5** | ~0.6–0.75s ([Artificial Analysis](https://artificialanalysis.ai/models/claude-4-5-haiku/providers)) | ~94–95 tok/s | $1.00 | $5.00 | Better grounding/instruction-following, 200K context, no rate-limit spikes | Live gate + answer | **Actual prod default (live)** |
| **Claude Sonnet 5** | slower — adaptive-thinking blocks add latency; explicitly flagged in `docs/research/` as not fit for the speak path | not benchmarked here (reasoning-oriented, not throughput-oriented) | $2.00 (intro→$3.00 after 2026-08-31) | $10.00 (intro→$15.00 after) | Near-Opus; best structured/JSON reliability | Post-meeting artifact / high-stakes async | **Actual prod default (post-meeting)** |
| Ollama (`llama3.2`, local) / stub | instant (local) / n/a | hardware-dependent / n/a | $0 | $0 | Lower (small local model) / deterministic non-model | Offline dev, zero-key demo | Dev-only |

**Read:** Cerebras wins on speed+cost *if* someone wires it in — 170ms TTFT
beats everything else here by 4–6×, and it would run about half the live-LLM
cost of Haiku. But at 8 answers/call the live-LLM line is already <2 cents —
**adopting Cerebras is a latency play, not a cost play.** Groq is the
measured-in-repo cheap/fast alternative already coded but disabled in prod
(free-tier rate limits were the reason it's not the default). Sonnet stays
off the live path for the same reason noted in the repo's own research: its
extended-thinking latency is wrong for a spoken answer.

---

## 8. Sensitivity — what moves the number

| Lever | Base case | Stress case | Effect on 30-min total |
|---|---|---|---|
| **Recall Output Media surcharge** (unverified — see §2.1) | billed at base $0.50/hr | +50–100% premium | Recall line: $0.325 → $0.49–$0.65; total: $0.40 → $0.57–$0.73 |
| **App Runner Frankfurt rate** (assumed = Ireland/US tier) | $0.064/vCPU-hr, $0.007/GB-hr | ±20% regional variance | Marginal per-meeting line: $0.032 → $0.026–$0.038 (immaterial) |
| **Live answers per call** | 8 | 16 (chattier meeting) | LLM live line roughly doubles (~+$0.01–$0.02); total moves <5% |
| **LLM engine choice** | Haiku live / Sonnet post | Cerebras or Groq live+post (cheapest) | Total: $0.40 → ~$0.37 — a ~7% swing, dwarfed by Recall/GPU |
| **Photoreal GPU SKU** | RunPod L4, $0.39/hr | RTX 4090, $0.34/hr, OR a reserved/on-demand box for reliability (~$0.80–$1.00/hr, e.g. AWS g6.xlarge L4) | Add-on: $0.17–$0.24 (spot) vs. $0.40–$0.50 (reserved/SLA) |
| **Meeting volume (fixed-cost allocation)** | 100 meetings/mo | 20 meetings/mo (early stage) | AWS fully-loaded allocation: $0.15/meeting → $0.75/meeting (still small vs. Recall) |

**Bottom line:** Recall.ai and (if enabled) the photoreal GPU are the only
two levers that move the total by more than a few percent. The brain
(Cerebras/Groq/Haiku/Sonnet) is now a rounding error at this call volume —
the opposite of the old Anam-era model, where the avatar face was ~90% of
the cost.

---

## 9. Source log

- Recall.ai 2026 pricing: https://www.recall.ai/blog/new-recall-ai-pricing-for-2026 ,
  https://www.recall.ai/pricing , https://docs.recall.ai/docs/calculating-usage ,
  https://docs.recall.ai/docs/stream-media
- Cerebras pricing: https://www.cerebras.ai/pricing , third-party trackers
  (deploybase.ai, costbench.com) for the unpublished Llama-3.3-70B rate
- Cerebras latency/throughput: https://artificialanalysis.ai/models/llama-3-3-instruct-70b/providers
- Groq pricing: https://groq.com/pricing
- Anthropic (Haiku/Sonnet) pricing: https://www.tldl.io/resources/anthropic-api-pricing ,
  https://pricepertoken.com/pricing-page/model/anthropic-claude-sonnet-4.5 ,
  https://artificialanalysis.ai/models/claude-4-5-haiku/providers
- AWS App Runner pricing: https://aws.amazon.com/apprunner/pricing/
- RunPod GPU pricing: https://gpuvec.com/providers/runpod , https://northflank.com/blog/runpod-gpu-pricing
- ElevenLabs pricing: https://elevenlabs.io/pricing
- WorkOS pricing: https://workos.com/pricing
- Anam pricing: https://anam.ai/pricing
- Internal: `backend/app/config.py`, `backend/app/llm.py`, `docs/ARCHITECTURE_CURRENT.md`,
  `docs/research/llm-options-for-live-laura.md`, `gpu/README.md`,
  `avatars/laura/about/provider_cost_and_replacement.md` (its Cerebras-is-live claim was CORRECT — verified against App Runner 2026-07-10)
