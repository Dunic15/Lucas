# Advisory-vertical spec: Laura for the RIA client-review meeting

> **What this is.** The product spec for Laura's first *committed* vertical, per the
> whitespace recommendation in [`YC-NICHE-EXPANSION.md`](YC-NICHE-EXPANSION.md) §5:
> **Wealth / financial advisory**: the only candidate niche that scored 5/5 on
> *whitespace × meeting-fit × ingestion*. It turns that recommendation into a concrete
> build: the buyer, the meeting, the 4–6 actions mapped to **real code seams** (shipped
> vs. to-build), the knowledge pack, the Wealthbox/Redtail write design, the consent
> architecture that hedges the *Otter.AI* wiretap risk, and a Now/Next/Later order.
>
> **Grounded in the code, 2026-07-14:** join+ground = Recall + RAG (shipped); deterministic
> live flag = [`brain.py`](../../backend/app/brain.py) `missing_step` + [`autopilot.py`](../../backend/app/autopilot.py) (shipped);
> email = [`actions.py`](../../backend/app/actions.py) `send_email` (shipped, draft→approve→send);
> Calendar/Gmail write = [`executor.py`](../../backend/app/executor.py) + [`google_client.py`](../../backend/app/google_client.py)
> (shipped, `NATIVE_EXECUTOR` flag-off); approve→execute loop = dashboard (#199–#203).
> **The one net-new build is the CRM/system-of-record write (Wealthbox/Redtail).**
> **Confidence:** the market/WTP/whitespace facts are the verified expansion doc; the
> action→seam mapping is grounded in the current code; the field mappings and design are
> synthesis to validate with one design partner (marked ⓘ).

---

## 1. The avatar in one line

> **"Vera joins your client-review calls, answers grounded in your firm's planning
> process, flags the review step you're about to skip, and does the paraplanner work
> after, notes and next-review in your CRM, the review summary drafted, the follow-up
> scheduled, so nothing slips between meetings."**

*(Vera = placeholder, follows the repo's human-first-name pattern: laura/cedric/marcus/duccio.
"Vera" ≈ truth/verification; swap freely.)*

**What Vera is NOT (the scoping that keeps her legal; see §6):** not an advice-giver, not a
recommender of securities, not a fiduciary actor. She **captures, flags, and executes admin**.
Every advice-shaped output is drafted for the human advisor to own.

---

## 2. Buyer, meeting, and the human being displaced

| | |
|---|---|
| **Target buyer** | Founder/principal of a **Registered Investment Advisor (RIA)** or financial-planning firm, **solo–50 advisors**. Owner-operator → fast buy, no enterprise procurement. |
| **The meeting Laura sits in** | The **client annual/periodic review** (recurring, on Zoom/Meet/Teams) and the **prospect discovery** meeting. Both are dense with follow-up admin. |
| **The expensive human displaced** | The **paraplanner**: ~**$45–75K/yr** (Glassdoor $74K, Indeed $61K, Salary.com $47K, ZipRecruiter $65K; verified). Their job *is* Laura's action set: schedule client meetings, take/transcribe notes, prep the annual review, generate reports, keep compliance records. |
| **Why this buyer over others** | Advisors keep **written process docs** (review checklists, KYC, IPS templates) → cheap ingestion; the meeting is a real recurring video call → meeting-fit; the seat is empty → whitespace (see §3). |

---

## 3. Why the seat is empty (the whitespace, restated for this vertical)

Verified in [`YC-NICHE-EXPANSION.md`](YC-NICHE-EXPANSION.md) (3-0):
- **Zocks / Zeplyn / Jump / Powder** join the advisor's meeting and write the CRM; but they are
  **explicitly SILENT notetakers** ("passive participants… capturing in the background").
- **AviaryAI / Fini** *speak*, but do **outbound / support** calls; not the advisor's own client
  meeting.
- YC finance directory (through S'26): **no speaking voice/avatar meeting participant** found.

→ The *silent-join + CRM-write* lane is taken. The **speaking, grounded, live-flagging vertical
participant** is free. **That, plus the depth of the SoR write, is the moat**: not the face, not
generic automation, not the silent notetaker (all commoditized).

---

## 4. The 4–6 actions: mapped to real code seams

Legend: ✅ shipped · ◐ shipped but flag-off / needs vertical wiring · 🔨 net-new build.

| # | Action | Code seam today | Status | Vertical work to do |
|---|---|---|---|---|
| **1** | **Join + answer grounded** from the firm's planning process/product menu | Recall client + RAG over `avatars/vera/knowledge/*` | ✅ | Author the advisory knowledge pack (§5). |
| **2** | **Flag a missing review step LIVE**: stale risk tolerance, missing beneficiary, KYC gap, no updated goals | [`brain.py`](../../backend/app/brain.py) `missing_step` + [`autopilot.py`](../../backend/app/autopilot.py) deterministic closing intervention | ✅ | Define the advisory **must-cover checklist** as the missing-step ruleset (deterministic, not LLM-judgment). |
| **3** | **Draft the review summary + send the recap email** | [`actions.py`](../../backend/app/actions.py) `send_email` (draft→approve→send) | ✅ | Advisory recap template + "what we covered / action items / next review" structure. |
| **4** | **Schedule the next review + any follow-up** | [`executor.py`](../../backend/app/executor.py) + [`google_client.py`](../../backend/app/google_client.py) Calendar write | ◐ flag-off | Flip `NATIVE_EXECUTOR` for this action behind the approval loop; add cadence rules (annual/semi-annual). |
| **5** | **🔨 Write the CRM / system-of-record**, meeting notes, action items, next-review date, flags | *none, new* | 🔨 **the build** | New `wealthbox_client` / `redtail_client`; field mapping (§7); write behind approve→execute. |
| **6** | **Route a compliance/paperwork task to ops**: e.g., beneficiary form, Reg BI disclosure, missing doc | [`actions.py`](../../backend/app/actions.py) `post_to_slack` / email | ✅ | Owner-routing rules; ties to action #2's flags. |

**The single highest-leverage build is #5**: and per [`YC-TRENDS-2022-2026.md`](YC-TRENDS-2022-2026.md) §0,
**depth of the write (correct field mapping) is the moat, not the write itself.** CRM-write alone
is now table-stakes (the silent notetakers do it); Laura's edge is *speaking + grounded + live-flag
+ deep vertical write*, together.

---

## 5. The knowledge pack (ingestion: the moat's raw material)

⚠ From [`WEDGE.md`](WEDGE.md): Laura's usefulness is gated on real, written process docs.
Advisory scores well here, but **validate the design partner actually has these written down**, not
in the advisor's head (riskiest assumption #3, §8).

What Vera ingests into `avatars/vera/knowledge/*.md` (re-run `backend/scripts/ingest.py` after edits):
- The firm's **annual-review checklist / agenda** → becomes action #2's must-cover ruleset.
- **KYC / suitability / risk-tolerance** questionnaire + refresh cadence.
- **IPS (Investment Policy Statement) template** + what triggers an update.
- **Reg BI / disclosure** language and when it's required (drives action #6, never advice).
- **Recap / review-summary templates** (action #3).
- **CRM field map** for Wealthbox/Redtail (action #5, §7).
- Per-client context is **session-injected** (like Cedric's per-meeting brief), not baked into the
  persona; keeps PII out of the shared pack.

---

## 6. Compliance architecture (the *Otter.AI* hedge: a hard constraint, NOT optional)

Per [`YC-NICHE-EXPANSION.md`](YC-NICHE-EXPANSION.md) §4, the dominant risk is **legal, not
competitive**: *In re Otter.AI Privacy Litigation* (N.D. Cal., MTD argued May 2026) turns on whether
a meeting bot is **"a tool for the host"** or **"a separate third party"**: Laura's exact "third
participant" framing. Ship these **before** the vertical launch:

1. **Spoken disclosure at join**: Vera says, on entry, that she's an automated assistant recording
   for the advisor (satisfies California's bot-disclosure rule *and* strengthens the "tool-for-host"
   posture).
2. **All-party consent capture**: record that every participant was notified; configurable per
   state (two-party-consent states).
3. **Host-owned data**: transcripts/notes belong to the advisor's firm; Laura is the firm's tool,
   not an independent party.
4. **No training on transcripts**: already a repo hard-constraint (transcripts are PII, memory-only,
   never logged). Make it explicit and contractual for the advisory tier.
5. **Advice firewall**: Vera never recommends securities/allocations; advice-shaped outputs are
   *drafts for the advisor*. Keeps her off SEC/FINRA fiduciary + licensing (documentation regime, not
   a licensing wall).

This architecture is **vertical-agnostic**, it also unlocks insurance (§ the hedge niche), so it's
the right thing to build once, first.

---

## 7. The CRM write: Wealthbox / Redtail design (ⓘ synthesis, validate w/ partner)

The two dominant RIA CRMs are **Wealthbox** and **Redtail** (Orion). Design:
- **New client module** `backend/app/wealthbox_client.py` (+ `redtail_client.py`), mirroring the
  `google_client` / executor pattern; every write goes **through the approve→execute loop** (#200–203),
  never fire-and-forget.
- **Field mapping (the depth that is the moat):**
  | Meeting output | Wealthbox/Redtail target |
  |---|---|
  | Review notes | Contact **Note** (dated, tagged "Annual Review") |
  | Action items | **Tasks** / **Workflows** (owner + due date) |
  | Next review date | **Opportunity/Event** or recurring task |
  | Missing-step flags (§4 #2) | Task flagged "compliance" + routed (§4 #6) |
  | Risk-tolerance / IPS change | Note + task to update the record |
- **Auth:** OAuth per firm, token in SSM (`/laura/prod/…` pattern), like the Google client.
- **Idempotency + audit:** every write logged with the approving human + timestamp (also feeds the
  action-provenance dashboard: Laura's positioning moat, per memory `laura-product-positioning`).

---

## 8. The riskiest assumptions to validate with the first design partner

1. **Trust/adoption**: will advisors let a *speaking* bot into a client review? Does its presence
   change the client's candor? (Silent notetakers dodge this; Vera's whole edge invites it.)
2. **WTP for execution**: will firms pay for the follow-up execution, or is it "nice to have" once
   Zocks/Zeplyn add a talking mode? Price against the **paraplanner salary displaced**, not per-seat.
3. **Ingestion reality**: does the firm have a *written* review checklist/IPS, or is it tacit? If
   tacit, onboarding cost kills the deal; screen for it in the first call.
4. **CRM-write acceptance**: will compliance let an AI write to the book-of-record CRM? The
   approve→execute loop + audit trail is the answer; verify the partner's compliance officer agrees.
5. **Consent posture**: does the spoken-disclosure + host-tool architecture actually keep Vera on
   the safe side of the *Otter.AI* question for this partner's states?

---

## 9. Build order (Now / Next / Later)

**NOW (unblocks everything, vertical-agnostic):**
- **Compliance/consent architecture** (§6): spoken disclosure, all-party consent, host-owned data,
  advice firewall. *Hard constraint; ship before any advisory pilot.*
- **Author the advisory knowledge pack** (§5) + wire the **must-cover checklist** into `missing_step`
  (§4 #2). Reuses the shipped deterministic-flag path.

**NEXT (the differentiating build):**
- **`wealthbox_client` + field mapping** (§7), behind approve→execute. *The single highest-leverage
  item; the deep vertical write.*
- Flip **Calendar write** (#4) on for advisory behind the approval loop.
- Advisory **recap + review-summary templates** (#3).

**LATER (after first design partner signal):**
- **Redtail** client (second CRM) + **insurance AMS** writes (Applied/EZLynx); the hedge niche
  reuses the same consent architecture + review-meeting motion.
- Cadence automation (annual/semi-annual review triggers), compliance-doc routing rules (#6).

---

## 10. So-what / cross-links

- This spec operationalizes [`YC-NICHE-EXPANSION.md`](YC-NICHE-EXPANSION.md) §5's recommendation.
  The **behavioral whitespace** (speaking + grounded + live-flag) is the reason to build it; the
  **CRM-write depth** is what makes it defensible; the **consent architecture** is the hedge that
  outranks any competitor risk.
- Insurance brokerage (the §2 hedge in the expansion doc) is a **near-free second vertical** from
  here; same consent architecture, same review-meeting motion, swap Wealthbox→Applied/EZLynx.
- The **second mining pass** is done ([`YC-NICHE-EXPANSION-II.md`](YC-NICHE-EXPANSION-II.md), 2026-07-14,
  105 agents): it hunted logistics/construction/events/CS/HR-ops/education and found **none beats
  Wealth/advisory** on whitespace × wallet × meeting-fit. CS is native-crushed, construction has the
  Procore incumbent, freight/events run on phone/email, HR-ops+education returned zero evidence. **This
  spec stands as the committed vertical.**

_Grounded in the code 2026-07-14. Market facts = verified expansion doc; action→seam mapping =
current repo; field mappings + design = synthesis (ⓘ), validate with the first RIA design partner._
