# Provider Cost and Replacement Notes

**Owner:** Product/engineering
**Applies to:** Decisions about Groq, Recall, Anam, ElevenLabs, and open-source alternatives
**Last reviewed:** 2026-07-03

## Cost structure

The main live meeting cost drivers are:

- Recall.ai bot time and output media variant
- avatar/voice provider minutes
- backend compute while the service is running
- LLM token usage
- transcription provider usage if not using built-in Recall transcription

LLM tokens are usually not the dominant cost for a 30-minute meeting. The avatar
provider and always-on backend compute are usually larger cost drivers.

## Current provider roles

Recall.ai is the meeting transport: it joins Zoom, Google Meet, and Microsoft
Teams, receives meeting data, and streams Laura's output media back into the
meeting.

Groq is the current live brain provider. It is used for low first-token latency
with `llama-3.3-70b-versatile`.

Anam is the current face and voice provider. In the current stack it should be
treated as a swappable mouth, not as the brain.

ElevenLabs is not required for the current live meeting path. The current live
voice is Anam's persona voice.

## Replacing Anam

Replacing Anam should not require rewriting the backend brain. The replacement
should keep the same contract: the brain produces text, and the avatar page or
renderer turns that text into audible/visible speech.

The lowest-risk replacement path is:

1. Keep Recall.ai for meeting entry and output media.
2. Replace the Anam avatar page with a self-hosted avatar renderer.
3. Keep the existing RAG and brain pipeline unchanged.
4. Test answer quality separately from visual quality.

Open-source options previously identified as candidates include browser 3D
avatars with real-time lip sync, MuseTalk-style GPU lip-sync, and NVIDIA
Audio2Face-style 3D animation. Those are avatar-rendering decisions and should
not change how Laura retrieves knowledge or writes answers.

## Model quality decisions

If Laura misunderstands the question, first check transcription quality and
retrieval. If Laura retrieves the wrong chunk, changing Groq to another model
will not fix the root cause.

If retrieval is correct but the reasoning is weak, then compare models. Groq is
optimized for speed. Claude or another stronger model may provide better
reasoning, but could increase latency and cost.

## Common questions

**"Can we lower cost by removing Anam?"**
Yes, removing paid avatar minutes is likely the largest savings opportunity, but
the replacement still needs TTS, lip sync, rendering, hosting, and operations.

**"Can we rebuild Recall ourselves?"**
Technically possible, but not recommended for the current stage. Recreating
cross-platform meeting entry, transcript capture, output media, reconnects,
calendar handling, and platform quirks is much larger than replacing the avatar
renderer.

**"Should we run everything on AWS?"**
The backend can run on AWS App Runner, but GPU avatar rendering should use a GPU
service such as ECS/EC2 GPU instances or a dedicated GPU host. App Runner is not
the right place for heavy GPU inference.
