# Multi-party meeting intelligence: research synthesis (2026-07-08)

How to make Laura smart in meetings with 3+ people: what research and production
systems actually do, and the ranked shortlist for our stack. Five research angles
(turn-taking, addressee detection, products, embodied-agent literature, LLM
prompt patterns), all web-sourced; URLs inline.

## The market seam (why this matters)

**Nobody today combines a speaking voice with active multi-party facilitation.**
- The big three are text-only in-meeting: Zoom AI Companion (panel Q&A), Google
  Meet "Ask Gemini" (private text side panel), Teams Facilitator (chat + agenda
  timers). Zoom's speaking "Zoomie" is promised mid-2026, not shipped.
- The only shipped speakers: Otter Meeting Agent (wake-word "Hey Otter",
  Zoom-first) and MeetGeek voice agents; neither does facilitation (round-robins,
  quiet-participant nudges, named floor-passing). Talk-time/inclusion products
  (Read.ai, Equal Time) are visual-only.
- No product verified to voice-address a participant by name to hand them the
  floor. That's the open seam Laura can own.
  (Sources: otter.ai/blog/otter-meeting-agent…, support.microsoft.com Facilitator
  docs, workspaceupdates.googleblog.com Ask Gemini, news.zoom.com EC26.)

## Turn-taking: what actually works

- **Production stacks are all dyadic.** LiveKit turn-detector, Pipecat smart-turn
  v3 (~8M params, 12ms CPU), Deepgram Flux (~260ms EOT); small audio/semantic
  end-of-turn classifiers wrapped in tunable silence heuristics. None ships
  multi-party turn-taking; in meetings the pattern is diarization + direct-address
  gating (Telnyx guide: agent silent until named).
- **Multiparty turn prediction is research-grade**: triadic VAP (arXiv 2507.07518,
  86.8% next-speaker acc.) exists but state space explodes past 4-5 speakers; no
  production deployment. Not worth building on yet.
- **LLMs are near chance at "who speaks next / who is addressed" from text**
  (GPT-4o ~chance on addressee benchmark arXiv 2501.16643; ~12% next-speaker acc.
  in self-selection turns). Don't ask the model "who's next".
- **The winning architecture: a cheap always-on gate in front of the expensive
  responder, with silence as the default output class.** "Inner Thoughts" (CHI
  2025, arXiv 2501.00383): agent continuously scores its *motivation to speak*
  (relevance, info gap, urgency) and speaks only above threshold; beat
  next-speaker prediction on all 7 metrics, preferred by raters 82%. LlamaPIE:
  small model as intervention classifier, large model only when triggered.

## Addressee detection: strongest signals, in evidence order

1. **Acoustic register**: people audibly shift into "computer talk" addressing a
   device; acoustic features beat lexical AND beat human judges (5-8% EER in
   Amazon/Alexa + Apple/Siri systems). Requires raw audio (we don't tap it yet).
2. **Explicit lexical address** (name, second person, imperatives); but only
   ~20% of natural multiparty turns mark the addressee explicitly.
3. **Dialogue context**: "was the agent just engaged / is this a follow-up to
   its answer?" is worth a further 20-40% false-alarm reduction (Apple, arXiv
   2411.00023). We already have this (`called` bypass + cooldown).
4. **ASR name corruption is the silent killer**: explicit-name cue response drops
   94.3% → 68% when the name is phonetically mangled (Meeting Delegate, arXiv
   2502.04376). Fuzzy name matching is cheap and high-yield.

## LLM prompt patterns for multi-speaker transcripts (validated)

- Chronological `RealName: utterance` lines + roster block in the prompt +
  agent knowledge kept structurally separate = the Meeting Delegate-validated
  format (we already do all three as of 2026-07-08).
- Platform-metadata names (Recall roster) >> acoustic diarization labels.
- Top documented failure modes to guard explicitly in the prompt:
  (a) answering questions directed at another named participant,
  (b) echoing earlier transcript content back (10-30% of delegate responses),
  (c) misattribution; users rate it "detrimental to group dynamics" (Microsoft
  meeting-recap study, arXiv 2307.15793).

## Embodied/social presence: what transfers to a Zoom tile

- **Mona Lisa effect**: on a flat 2D tile every viewer already perceives the
  avatar as looking at them; precise gaze targeting is impossible AND
  unnecessary. Footing must be expressed **verbally and temporally** instead.
- Mutlu et al. (HRI 2009) footing findings, verbal analogues:
  - *Acknowledgment has outsized payoff*: merely greeting/acknowledging
    bystanders made them like the robot significantly more; being ignored
    actively hurt. → greet joiners by name, acknowledge everyone at open/close.
  - *Turn-yielding is the strongest cue* (97% compliance): → end contributions
    cleanly, pass the floor by name ("Marco, you mentioned the timeline…").
  - *Never leave anyone in the overhearer state* → quiet-participant awareness.
- Furhat robot + LLM (arXiv 2503.15496): 92.6% addressee accuracy needed voice
  direction-of-arrival + face recognition; speaker-ID errors and latency were
  the fluidity killers; attribution quality and latency dominate everything.

## Ranked shortlist for Laura (implementability × impact)

1. **Fuzzy wake-word/name matching**: ASR mangles "Laura" (Lara/Lora/Loura) and
   participant names; exact-match gating loses a quarter of direct addresses.
   Trivial (edit-distance / phonetic variants in decision.py). *Highest yield.*
2. **Deference window**: room-open questions: wait ~1.5-2s on the partial
   stream; a human starting to answer cancels her. (= motivation gate + silence
   as default class.) Planned phase 1.
3. **Verbal footing package**: greet joiners by name (we have join events),
   named floor-passing, wrap-up nudge for quiet participants (roster minus
   transcript speakers). No product does this by voice; differentiator.
4. **Anti-echo + anti-misattribution prompt guards**: one line each in the
   stream system prompt; both are top measured failure modes.
5. **Talk-balance in the artifact**: per-person line counts (shipped today)
   → a "participation" line in the post-meeting artifact, Read.ai-style but
   ours is in the same product as the voice.
6. **Motivation-scored proactivity (later)**: score relevance/urgency of an
   unprompted contribution on the fast model before speaking (Inner Thoughts
   pattern) instead of binary SKIP.
7. **Audio-path upgrades (hold)**: acoustic directedness / VAP / pyannote need
   the raw-audio websocket + GPU; revisit when the photoreal GPU box lands.

## What we already shipped that the research validates

- Roster from participant events injected into the prompt (Meeting Delegate).
- `Name: line` transcript + speaker-named ask (DiarizationLM, Recall.ai docs).
- Vocative gate for other-addressed lines (Telnyx direct-address pattern).
- Barge-in + cooldown + called-bypass (dialogue-context signal, Apple).
- Per-person tracking in MeetingState → "what did Marco commit to?".
