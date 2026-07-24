# Cedric × ElevenLabs Agents — conversation-runtime pilot

**Status:** PR 1 (config + isolation) shipped. The **ElevenLabs agent EXISTS**:
"Cedric Meeting Pilot" (`agent_0801ky9qgd9cfk8aw3fgj8keytgp`), created
2026-07-24 by `backend/scripts/create_cedric_agent.py` (config-as-code,
idempotent re-runs update in place; signed-URL mint verified 200). Its id is
wired into `avatars/cedric/avatar.yaml`, so **three of the four dispatch
conditions are true in-repo — the env flag alone is the go-live switch** (off
everywhere today). Audio bridge (PR 2) not built yet: the agent is reachable
but no meeting audio flows to it.
**Owner decision:** Cedric is the ONLY pilot avatar. Laura, Petra and every
other avatar stay on the legacy pipeline, untouched, for the whole pilot.
**Last reviewed:** 2026-07-24

### Agent settings as created (see the script for the full source of truth)

Aligned 2026-07-24 with **SFF-Studio/UnderHeard-Voice** — the in-house
production ElevenLabs agent (its `docs/features/voice-agent.md` records the
battle-tested "why" per knob). Verified persisted via GET after PATCH.

| Setting | Value | Why |
|---|---|---|
| LLM | `claude-sonnet-4-6`, temperature 0.4, **max_tokens 200** | Underheard prod pick; uncapped tokens measurably slowed responses. (They trial Qwen; it "sometimes gets lost" — not for the pilot.) |
| Voice / TTS | `cjVigY5qzO86Huf0OWal` (Eric), **`eleven_v3_conversational`**, out `pcm_16000`, `optimize_streaming_latency: 2` | v3 conversational = multilingual + best quality, and the one multilingual model the API accepts on en-default agents (flash/turbo v2_5 are rejected: "English Agents must use turbo or flash v2"). Latency dial 3 caused audible breakup on first words. |
| ASR input | `pcm_16000` | Recall's exact stream format — no transcoding |
| Languages | `en` default + `it` preset | Team code-switches EN/IT; per-call `language` override must match a preset |
| Turn-taking | eagerness `patient`, timeout 7 s, `interruption_ignore_terms` (16 EN+IT backchannels), `transcribe_on_disabled_interruptions: true` | Backchannels ("yeah", "sì") must not cut Cedric off; real barge-in still interrupts. Patient (not Underheard's `normal`): a meeting avatar waits its turn. |
| First message | disabled | The legacy join self-intro stays the ONE greeting |
| Access | private, `auth.enable_auth: true` → signed-URL-only | Key stays server-side (SSM `/laura/prod/ELEVENLABS_API_KEY`) |
| **Overrides** | client may override **only** `prompt.prompt`, `first_message`, `language` | The Underheard per-call pattern PR 2 uses: the relay injects the per-meeting prompt/context via `conversation_initiation_client_data`; LLM/tools/knowledge are locked server-side |
| Tools | none yet | Pilot 1 is conversation-only; client tools land in PR 4 |
| Prompt | yaml `persona_prompt` verbatim + MEETING PILOT RULES (multiparty discipline, no bare-yes approvals, never claim "done", grounding honesty, untrusted-data rule for injected context, EN/IT, short answers) | Untrusted-data framing adopted from Underheard's prompt-injection defense |

**PR 2 must follow the Underheard bootstrap shape:** signed URL + overrides
returned by the backend, session started with
`conversation_initiation_client_data: {conversation_config_override: {agent:
{prompt, first_message, language}}, dynamic_variables: {...}}`. Two of their
hard-won lessons: any `{{var}}` referenced anywhere must be present in
`dynamic_variables` (a missing one kills the conversation at second zero),
and wrap every injected value in explicit BEGIN/END UNTRUSTED DATA blocks.

## What this is

Hand Cedric's live-meeting *conversation* — STT, turn detection, interruption,
the spoken reply — to a private ElevenLabs Agent, while **everything that makes
the product the product stays in the Laura backend**: meeting context, Company
Brain retrieval, `queue_action`, canonical actions, approvals, receipts, audit.
The bet: ElevenLabs' turn-taking + streaming voice buys conversational quality
the legacy Deepgram→gates→brain→TTS chain approximates, without surrendering
the Action Control Plane (the actual moat).

```
Recall bot ─ mixed 16k PCM ─► CF relay (laura-ears worker, NEW route)
                                 │  ring buffer + Meeting Director gate
                                 ▼
                        ElevenLabs Agent: Cedric
                        (STT · turns · interrupts · LLM · voice)
                                 │  client tool calls ─► Laura backend
                                 ▼      (context, brain, queue_action)
                        audio chunks ─► /talk page (cedric) ─► Recall ─► meeting
```

## Why this is feasible in THIS codebase (verified 2026-07-24)

Every structural piece the plan assumes already exists:

- **Audio ingress exists.** Recall already streams the meeting's mixed s16le
  16 kHz audio to the Cloudflare relay (`relay/laura-ears/worker.js`) — the
  Gemini-ears path. The ElevenLabs route is a *sibling route* on that worker,
  not new plumbing (`recall_client._ears_ws_url` builds
  `/realtime/recall-audio/{capability}`; the pilot adds
  `/realtime/cedric-elevenlabs/{capability}`).
- **Per-avatar runtime routing exists.** `gemini_ears.mode_for_avatar()`
  already resolves a per-avatar brain choice at bot-creation time — the exact
  pattern `elevenlabs_agent.runtime_for_avatar()` (PR 1) mirrors.
- **The gate exists.** Cedric ships `require_wake_word: true` + ASR wake
  variants (`cedric/cedrick/sedric/…`) with an exact-match wake path; Recall
  partials with speaker labels feed the backend today. The Meeting Director is
  a state machine over signals that are already flowing.
- **The renderer exists.** `/talk?avatar_id=cedric` loads `cedric.glb`; the
  pilot adds a streaming-audio branch next to the `{type:"speak",text}` one.
- **The action plane exists.** `queue_action` → typed/untyped → approval →
  executor → receipt is live in prod. The agent's client tools call it; they
  never touch Asana/Gmail/Calendar directly.

## Hard rules (from the live-meeting contract + owner rules)

1. `ws/<conversation_id>`, `{type:"speak",text}`, `recall_client`/`anam_client`
   signatures unchanged — the pilot is additive, behind flags.
2. Demo stays key-free; every new path is inert without keys/flags.
3. Transcripts + meeting audio are PII: never logged, never persisted outside
   the store. No audio, keys, signed URLs, or brief content in logs.
4. Meter safety: the fallback path must never create a second Recall bot.
5. No secrets in git; the ElevenLabs API key stays server-side (SSM), the
   browser/relay only ever see short-lived signed URLs.

## Isolation model (PR 1 — SHIPPED)

Dispatch to ElevenLabs requires **all four**, else legacy (fail-closed):

| # | Condition | Where |
|---|---|---|
| 1 | `ELEVENLABS_AGENT_RUNTIME_ENABLED=true` | env (default **false**) |
| 2 | avatar id ∈ `ELEVENLABS_AGENT_AVATAR_ALLOWLIST` | env (default `cedric`) |
| 3 | `conversation_runtime: elevenlabs_agent` | `avatars/<id>/avatar.yaml` |
| 4 | non-empty `elevenlabs_agent_id` | `avatars/<id>/avatar.yaml` |

- Resolver: `backend/app/integrations/elevenlabs_agent.py` (never raises;
  any failure → `"legacy"`).
- Snapshot: `store.create()` freezes `session.conversation_runtime` +
  `session.elevenlabs_agent_id` at session start — a yaml/flag edit mid-call
  never migrates a live meeting. Deliberately **in-memory**: after a backend
  restart the relay bridge is dead, so rehydrated sessions fall back to
  legacy (the runtime that still works). Guarded by tests.
- Cedric's yaml opts in but ships with `elevenlabs_agent_id: ""` → inert even
  if someone flips the env flag before the agent exists.
- Tests: `backend/tests/test_conversation_runtime.py` (22, all green).

New avatar.yaml fields (unknown values normalize to safe defaults):
`conversation_runtime` (`legacy`|`elevenlabs_agent`), `elevenlabs_agent_id`,
`voice_multiparty_mode` (`off`|`wake_word_gate`),
`voice_actions_mode` (`off`|`read_only`|`prepare_only`).

## Remaining phases → PR sequence

Branches stack; each is draft-PR-only (owner merges — PR-only rule).

### PR 2 — `spike/cedric-elevenlabs-audio` (audio bridge, ~2 days)
- `POST /internal/voice-agent/bootstrap`: capability-checked, Cedric-only,
  resolves the session, calls ElevenLabs **get signed URL** server-side,
  returns `{signed_url, conversation_id, runtime}`. Key never leaves the
  backend.
- Relay route `/realtime/cedric-elevenlabs/{capability}` on
  `relay/laura-ears/worker.js` — per-session Durable Object bridging three
  sockets: Recall audio in, ElevenLabs WS, browser audio out. 2 s PCM ring
  buffer (wake word arrives *after* the phrase started; replay the buffer on
  gate-open so the agent hears the whole sentence).
- `/talk` streaming-audio branch (`voice_runtime=elevenlabs_agent`): decode
  chunks, jitter buffer, play, drive the speaking animation; flush instantly
  on `interruption`. Lip-sync precision is explicitly NOT a goal yet.
- A script (`backend/scripts/create_cedric_agent.py`) that creates/updates
  the "Cedric Meeting Pilot" agent from avatar.yaml persona + pilot rules,
  voice `cjVigY5qzO86Huf0OWal` — the agent config lives in code review, not
  in a dashboard.

### PR 3 — `spike/cedric-elevenlabs-multiparty` (Meeting Director, ~2 days)
- Gate state machine per session: `closed` by default; opens on Cedric wake
  word (exact-match incl. ASR variants) or an active-interlocutor follow-up
  (~15 s window); closes on turn end / expiry / hand-off to another human.
- `session.voice_owner = "elevenlabs" | "legacy"`: while ElevenLabs owns the
  voice, the legacy live path still ingests transcripts (MeetingState,
  attribution, artifact, action extraction) but **never generates a spoken
  answer**; Gemini ears forced off for the session. Zero double-voice.
- Backchannels ("yeah", "mhmm") never open the gate; human→human questions
  ("Ananth, cosa ne pensi?") send a `contextual_update` to the agent, not a
  turn.
- **Bare "yes" never approves anything** — approvals stay in the dashboard.

### PR 4 — `spike/cedric-elevenlabs-tools` (client tools, ~1.5 days)
Client tools over the agent WS, relayed to the backend: `get_meeting_context`,
`search_company_knowledge`, `get_available_actions`, `queue_action` (creates a
canonical action, `approval_required: true`, never executes). Org+session
bound; duplicate tool calls must not mint duplicate actions; a tool timeout
must surface as "I'll check" — never a false "done".

### PR 5 — `spike/cedric-elevenlabs-hardening` (~1.5 days)
Fallback (any bridge failure → drain audio queue, `voice_owner=legacy`,
legacy brain resumes, spoken one-liner about the hiccup; NEVER a second bot),
per-turn latency timestamps (gate→forward→first-chunk→playback), structured
turn logs (ids + timings only — no content), cost counter per session, soak
test, ops doc. **Update `avatars/cedric/about/` + Laura's
`laura_architecture.md` if her docs mention runtimes, then
`python3 backend/scripts/ingest.py cedric`** (self-knowledge rule).

## Known risks (annotations the original plan under-weighted)

1. **Echo / self-hearing (top technical risk).** Recall streams *mixed* room
   audio — while Cedric speaks, his own voice is in the ingress stream. For
   barge-in the bridge must keep forwarding during agent speech, so the agent
   can hear itself → self-interruption loops. Mitigations, in order: forward
   only while the gate is open AND the active speaker isn't Cedric (we have
   speaker labels from Recall partials, ~real-time); hard-mute ingress during
   agent playback minus a barge-in energy detector; investigate Recall
   separate-streams (per-participant audio) to exclude the bot's own output.
   Must be proven in the first live test before building PR 3 polish.
2. **Burst replay vs VAD.** Gate-open replays ~2 s of buffered PCM faster than
   real time; ElevenLabs VAD/turn detection may mis-segment. Validate early
   (may need paced replay at 1.2–1.5×).
3. **Grounding honesty regression.** The legacy path carries months of
   capability-grounding work (#421-bis: honest "I can't", read-vs-write
   discipline, no false "done"). The agent's prompt + tool results must
   re-encode those rules or Cedric regresses live. Port the rules into the
   agent prompt in PR 2 and test them in PR 4.
4. **Cost stacking.** ElevenLabs Agents is billed per conversation minute ON
   TOP of Recall per-minute + LLM. Before PR 2: confirm the current ElevenLabs
   plan covers Agents + this voice (the account already 402'd once on a voice
   tier), and put a $/min number in the finance model. A pilot that converses
   beautifully at negative margin is a demo, not a product.
5. **Latency probably fine, verify anyway.** Meet→Recall→CF→ElevenLabs→CF→
   browser→Recall→Meet adds hops; the acceptance bar (<1.5 s median to first
   audio) absorbs them only if the relay does no transcoding on the hot path.

## Acceptance gates (unchanged from the owner's plan)

Conversation: median question→first-audio < 1.5 s (p95 < 2.5 s), interruption
< 500 ms, 0 duplicate answers, 30-min stable session. Multiparty: < 5% replies
to human↔human talk, > 90% correct same-speaker follow-ups, 0 answers to
questions addressed to others, 0 ambiguous approvals. Actions: 100% of tool
calls create canonical actions, 0 direct writes, 0 false "done", 0 duplicates.
~30 scripted live scenarios across 1:1 / 3-person / 5-person rooms.

**Extend beyond Cedric only after all three pilot gates pass:** (1) measurably
better conversation, (2) no answers when humans talk to each other, (3) a
canonical action queued end-to-end without direct execution.
