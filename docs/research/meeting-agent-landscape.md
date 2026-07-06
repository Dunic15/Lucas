# Meeting Agent Landscape Research

_Last updated: 2026-07-06_

## Executive takeaway

Laura should not compete as another AI note taker or generic avatar. The strongest wedge is:

> A live process expert that joins meetings, stays mostly silent, and prevents teams from missing required process steps.

The avatar is the demo layer. The product value is process correctness, owner/action extraction, readiness scoring, and post-meeting workflow execution.

## Competitive landscape

### 1. Meeting assistants / note takers

Examples: Otter, Granola, Fathom, Fireflies, Zoom AI Companion, Microsoft Copilot, Google Gemini in Meet.

What they do well:

- transcription
- summaries
- action items
- searchable meeting memory
- CRM or productivity-tool sync
- post-meeting follow-up

Risk for Laura:

- If Laura is positioned as “AI meeting notes with an avatar,” it will be weak.
- These tools are mature and platform players can bundle generic summaries.

What Laura should copy:

- pre-meeting prep
- post-meeting action list
- CRM/project-management sync
- searchable meeting memory
- clean shareable artifacts

What Laura should avoid:

- generic meeting summaries
- over-sharing transcripts by default
- too much speaking during live meetings

Laura differentiation:

- live process intervention
- company-process grounding
- missing-step detection
- readiness score
- workflow-specific templates

### 2. Sales-agent tools

Examples: Otter Sales Agent, Gong, Clari/Copilot-style revenue intelligence, CRM assistants.

What they do well:

- pre-call account prep
- CRM context
- sales coaching
- follow-up email generation
- deal signals
- Salesforce/HubSpot updates

What Laura should copy:

- “pre-call brief” becomes Laura’s “process brief”
- live coaching becomes “process coaching”
- deal score becomes “process readiness score”
- CRM updates become onboarding/project updates

Laura differentiation:

- not only sales calls
- focused on process correctness in customer onboarding, implementation, procurement, security reviews, and approvals

### 3. Avatar infrastructure

Examples: Anam, Tavus, HeyGen, Keyframe Labs, Synthesia, ElevenLabs avatar/voice stack.

What they do well:

- realistic face/voice
- low-latency avatar rendering
- embeddable talking agents

Risk for Laura:

- Avatar realism is commoditizing.
- If Laura’s moat is only the face, suppliers can become competitors.

What Laura should copy/use:

- high-quality avatar output where it improves demos
- low-latency speech and turn-taking patterns
- fallback avatar modes

Laura differentiation:

- the avatar is replaceable; the brain/process layer remains Laura-owned

### 4. Meeting infrastructure

Examples: Recall.ai, LiveKit Agents, Pipecat.

What they do well:

- meeting bots
- realtime voice/video infrastructure
- STT/LLM/TTS pipelines
- WebRTC transport
- session orchestration

What Laura should copy/use:

- provider abstraction
- latency metrics
- health checks
- fallback routing
- clean session lifecycle

Laura already uses Recall for meeting bot/transcription/camera rendering. Do not rewrite unless there is a clear reason.

### 5. Speech intelligence

Examples: pyannote.audio, pyannoteAI, WhisperX.

What they do well:

- speaker diarization
- word-level timestamps
- speaker-turn alignment
- better post-meeting attribution

What Laura should use later:

- post-meeting speaker attribution
- action-owner extraction
- recurring speaker identity
- decision-maker presence

Do not put pyannoteAI in the live path yet. MeetingState is a higher priority.

## Recommended product roadmap

### Now

1. MeetingState intelligence layer
2. Customer onboarding process template
3. Transcript saved in post-meeting artifact
4. Structured artifact: summary, transcript, decisions, actions, missing steps, risks, readiness score, follow-up email
5. GPU cost controls and meeting-only runtime

### Next

1. Slack/email delivery of meeting artifacts
2. Google Calendar pre-meeting brief
3. Google Drive/Docs ingestion for process docs
4. CRM/Jira/Linear export
5. pyannoteAI post-meeting enhancement

### Later

1. Voiceprint-based recurring speaker identity
2. Multiple vertical templates
3. Admin dashboard for process templates
4. Enterprise controls: retention, consent, audit, SSO

## Useful open-source projects to study

### Pipecat

Why useful: realtime multimodal/voice AI pipeline patterns, provider abstraction, STT/LLM/TTS orchestration, VAD, metrics.

How to use: study architecture only. Do not migrate Laura now.

### LiveKit Agents

Why useful: realtime agent sessions, WebRTC, voice agent turn-taking, tools, dispatch, job/session management.

How to use: study patterns for session orchestration and fallbacks.

### pyannote.audio

Why useful: post-meeting diarization and speaker attribution.

How to use: later, after MeetingState and transcript artifact are working.

### WhisperX

Why useful: offline/backup ASR with word-level timestamps and alignment.

How to use: only if Recall transcript quality is insufficient or offline processing becomes needed.

## What to build because of competitor research

### Feature 1: Process Brief

Before the meeting, Laura should show:

- meeting goal
- relevant process docs
- required steps
- watch items
- suggested questions

### Feature 2: Process Readiness Score

After/during the meeting:

- onboarding readiness: 62%
- missing approvals
- missing owners
- unresolved blockers
- recommended next step

### Feature 3: MeetingState

During the meeting, Laura silently tracks:

- decisions
- owners
- deadlines
- risks
- missing steps
- open questions
- whether to intervene

### Feature 4: Artifact delivery

After the meeting:

- send follow-up email
- post Slack summary/checklist
- export tasks to Jira/Linear
- update CRM/customer record later

## Positioning

Bad positioning:

> AI avatar for meetings.

Better positioning:

> Laura is your company’s process expert, live in every meeting.

Best current wedge:

> Laura prevents teams from missing critical onboarding steps during customer calls.

## Sources to revisit

- Otter Sales Agent / Otter API and integrations
- Granola security/privacy notes
- Fireflies integrations
- Pipecat GitHub repo
- LiveKit Agents GitHub repo
- pyannote.audio GitHub repo
- WhisperX GitHub repo
- AWS EC2 start/stop and Instance Scheduler docs
