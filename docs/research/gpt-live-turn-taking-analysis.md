# GPT-Live turn-taking — analysis & integration verdict

**Date:** 2026-07-09
**Question:** OpenAI shipped GPT-Live (full-duplex voice) on 2026-07-08. Should Laura integrate its turn-taking approach?
**Verdict:** **No — do not integrate GPT-Live.** Harvest 3 patterns into Laura's existing architecture instead. Details below.

---

## 1. What GPT-Live actually does

Sourced from OpenAI's launch + press coverage (2026-07-08):

- **Full-duplex.** Listens and speaks *simultaneously* — not the turn-based "wait-for-silence" loop. Internally it "makes decisions many times per second: speak, listen, pause, interrupt, tool."
- **Back-channels while you talk.** Emits "mhmm" / "yeah" / "got it" mid-stream to signal attention, or stays quiet when you pause to think — without false end-of-turn triggers from background noise.
- **Split-model delegation.** The Live model holds the conversation; hard queries (search, deep reasoning) are handed to **GPT-5.5 in the background** and the result is woven back in when ready. User-selectable effort tiers: Instant / Medium / High.
- **Availability:** ChatGPT app only (iOS / Android / web) at launch — *not even the desktop app*. Developer **API is "planned," not shipped** — no docs, pricing, or latency numbers. Video/screen-share not supported yet.

## 2. How Laura's live path works today (the constraint that decides this)

Mapped from the code (`backend/app/main.py`, `brain.py`, `decision.py`, `frontend/talk.html`):

**Laura is not a duplex audio system.** The "live loop" is an **HTTP webhook fan-in + WS/SSE fan-out**:

```
human speaks
  → Recall.ai does ASR, POSTs transcript events → recall_webhook (main.py:2149)
  → partial events: barge-in / ack / backchannel only (main.py:2169)
  → final (endpointed) event: full speak-decision pipeline (main.py:2331)
  → answer_question_stream (brain.py:472), in-stream SKIP-gate
  → {type:"speak",text} pushed over /ws → browser (talk.html:582)
  → browser does local TTS + TalkingHead render
  → Recall bot captures the browser canvas+audio back into the meeting
```

Key properties relevant to a full-duplex swap:
- **No Laura-side audio at all.** ASR is Recall's; Laura only ever sees *text*. Output is *text* → browser TTS → canvas → Recall. There is no raw audio stream Laura owns end-to-end.
- **Turn-based on ASR finals.** Endpointing (~1–2s) is delegated entirely to Recall; Laura has no VAD.
- **Turn-taking = fixed timers:** deference window `1.8s` (`config.py:221`), unprompted cooldown `8s`, follow-up window `15s`. Not adaptive.
- **Barge-in already exists** on the TalkingHead page (`_should_barge_in` main.py:1913; client `interruptSpeech` talk.html:328). The **Anam page silently ignores stop** (no `stop`/`generation_id` handler, avatar.html:171) — known gap.
- **Split-model already exists:** Haiku on the live path (`brain_model_fast`), Sonnet on the background/post-meeting path. In-stream `SKIP` sentinel decides stay-silent-vs-respond (`brain.py:614`).
- **Back-channels/acks already exist** but are *fixed prewarmed lines* ("Mm-hm.", "Sure —"), not model-generated (main.py:2234, 2578).

## 3. Why integrating GPT-Live is a no (three independent reasons)

1. **There is no API.** OpenAI only *plans* to expose it. Today it's locked inside the ChatGPT consumer app (not even their own desktop app). Nothing to integrate — this alone settles the near term.

2. **Architectural mismatch with Recall.** GPT-Live is an audio-in/audio-out model that needs a **continuous bidirectional audio socket to the end user**. Laura's transport is Recall = transcript-webhook in, browser-canvas out. There is no per-participant raw-duplex audio path to hand GPT-Live. Adopting it means **re-architecting the entire media path off Recall** — the opposite of a bolt-on.

3. **Product-fit conflict.** GPT-Live is a *chatty, self-directing general assistant* that back-channels over you and decides its own turns. Laura's value is the inverse:
   - **grounded in process docs** (RAG) — GPT-Live answers from its own weights;
   - **silent unless addressed** (SKIP-gate) — GPT-Live volunteers;
   - **its own voice identity** (ElevenLabs) — GPT-Live is OpenAI's voice;
   - **controllable per-minute meter / cost model** — GPT-Live is opaque.

   Swapping it in throws away grounding, voice, silence-discipline, and cost control — i.e. Laura's whole differentiation. In a *multiparty meeting*, a bot going "mhmm" over live participants is **noise, not presence** — Laura's restraint is a feature, not a gap to close.

## 4. What to harvest instead (cheap, high-value, stays in current architecture)

| # | Pattern from GPT-Live | Concrete Laura change | Value / cost |
|---|---|---|---|
| **A** | "Decide many times per second" — act continuously, not on turn-end | **Act on Recall *partials*, not only finals.** Start speculatively drafting when confidently addressed on a partial; commit/cancel on the final. Closes the ~1–2s endpoint lag that dominates `wake→first_speak`. | High value, **medium risk** — scope carefully (false starts, wasted tokens). Prototype behind a flag. |
| **B** | Continuous turn-boundary modeling | **Make the deference window adaptive** (`1.8s` fixed → scale by speech rate / whether the last partial ended mid-clause). | Medium value, low cost. |
| **C** | Full-duplex = interruptibility is core | **Fix the Anam-page barge-in gap** — add `stop`/`generation_id` handling to `avatar.html` (already on backlog). | Low cost, correctness fix. |
| **D** | Effort tiers (Instant/Medium/High) → background frontier model | **Narrative, not code:** this *is* Laura's Haiku-live + Sonnet-background split. A frontier lab converged on Laura's design — good fundraise line. Optionally expose a per-avatar "effort tier" knob later. | Free (positioning). |

**Do NOT copy:** aggressive back-channeling while others are speaking. Correct for a 1:1 consumer assistant; wrong for a meeting avatar that should be unobtrusive.

## 5. Recommendation

- **Now:** log pattern **D** in the fundraise narrative; do the **C** barge-in fix (small, correct).
- **Next:** prototype **A** (partial-driven speculative drafting) behind a config flag on the live path — it's the one change that meaningfully attacks Laura's real latency floor. Measure `wake→first_speak` before/after; watch token cost and false-start rate.
- **Later / revisit trigger:** re-open the "adopt a full-duplex model" question only if **both** (i) GPT-Live (or an open equivalent) ships a real streaming API, **and** (ii) Laura's media path moves to a raw duplex audio transport — and even then, only as an *optional face*, never at the cost of RAG grounding + silence-discipline.

## Sources
- [Introducing GPT-Live — OpenAI](https://openai.com/index/introducing-gpt-live/)
- [OpenAI launches GPT-Live voice model series — SiliconANGLE](https://siliconangle.com/2026/07/08/openai-launches-gpt-live-voice-model-series-ahead-broad-gpt-5-6-release/)
- [OpenAI releases new voice models for more natural live conversations — TechCrunch](https://techcrunch.com/2026/07/08/openai-releases-new-voice-models-for-more-natural-live-conversations/)
- [GPT-Live Explained — Mervin Praison](https://mer.vin/2026/07/gpt-live-explained-full-duplex-chatgpt-voice-with-gpt-5-5-delegation/)
