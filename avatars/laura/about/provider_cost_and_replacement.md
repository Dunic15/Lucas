# Provider Cost and Replacement Notes

**Owner:** Product/engineering
**Applies to:** Decisions about Cerebras, Claude, Recall, ElevenLabs, Deepgram, Browserbase, and open-source alternatives
**Last reviewed:** 2026-07-20

## Cost structure

The main live meeting cost drivers are:

- Recall.ai bot time and output media variant
- backend compute while the service is running
- LLM token usage (small)
- transcription provider usage (Deepgram)
- ElevenLabs characters (effectively free at current volume: the plan covers
  millions of characters per month and a 30-minute meeting uses a few thousand)
- Browserbase browser minutes, only while a browse task actually runs
  (free-tier allowance today; a bounded task is seconds-to-minutes)

A 30-minute meeting costs roughly $0.40-0.80 all-in. There is no per-minute
face vendor cost: the avatar is an open-source in-browser renderer.

## Current provider roles

Recall.ai is the meeting transport: it joins Zoom, Google Meet, and Microsoft
Teams, receives meeting data, and streams Laura's output media back into the
meeting.

**Cerebras** is the current live brain provider (`gemma-4-31b` through an
OpenAI-compatible API), chosen for very low first-token latency. **Claude**
covers the rest: Haiku for clearly complex questions and as the automatic
fallback if Cerebras fails, Claude with native web search for fresh-information
questions, and Sonnet for the post-meeting artifact.

**Browserbase** is the supervised cloud-browser vendor behind the Sable
browser operator: one real Chrome session per browse task, created and
closed by the backend, always bounded and recorded. The visual planner that
reads the screen is **Claude** (`claude-opus-4-8`) on the existing Anthropic
account; a browse step costs roughly 1,600–1,800 input tokens plus ~200
output, i.e. well under a cent. The free Browserbase tier allows 3
concurrent sessions and no residential proxies, so bot-protected sites are
out of scope until a paid tier. The provider sits behind the
`BrowserProvider` seam, so any CDP-capable vendor could replace it without
touching the operator, policy engine, or planner.

The **face** is the open-source TalkingHead WebGL avatar (the `/talk` page) -
free, no vendor. Anam (the previous paid face vendor) is kept only as a
fallback page. The **voice** is ElevenLabs, synthesized server-side with word
timings for lip-sync; free edge-tts is the fallback. **Transcription** is
Deepgram nova-3 multilingual through Recall.

## Replacing a provider

Replacing any vendor should not require rewriting the backend brain. The
contract stays: the brain produces text, the avatar page turns it into
audible/visible speech.

The lowest-risk path for face upgrades (e.g. photoreal):

1. Keep Recall.ai for meeting entry and output media.
2. Swap the avatar page (`AVATAR_PAGE` env var selects `/talk`, `/photoreal`,
   or `/avatar`).
3. Keep the existing RAG and brain pipeline unchanged.
4. Test answer quality separately from visual quality.

A photoreal GPU track (MuseTalk-based, streaming a real face) is already built
and validated end-to-end on a stub engine; it costs about $1/hour only while a
meeting runs.

## Model quality decisions

If Laura misunderstands the question, first check transcription quality and
retrieval. If Laura retrieves the wrong chunk, changing the LLM provider will
not fix the root cause.

If retrieval is correct but the reasoning is weak, then compare models.
Cerebras is optimized for speed; complex questions already route to Claude.
The providers are pluggable by configuration, so swaps are cheap to test.

## Common questions

**"How much does a meeting cost?"**
Roughly $0.40-0.80 for 30 minutes, dominated by the Recall bot. The brain
tokens and voice characters are minor at current volume.

**"What does browsing cost?"**
Almost nothing at current volume: Browserbase session minutes on the free
tier plus a fraction of a cent of Claude vision tokens per step. The
expensive part of a browse task is human attention at the approval door,
which is by design.

**"Can we rebuild Recall ourselves?"**
Technically possible, but not recommended for the current stage. Recreating
cross-platform meeting entry, transcript capture, output media, reconnects,
calendar handling, and platform quirks is much larger than replacing the
avatar renderer.

**"Should we run everything on AWS?"**
The backend runs on AWS App Runner (`eu-central-1`). GPU avatar rendering
(photoreal) should use a dedicated GPU host, not App Runner.
