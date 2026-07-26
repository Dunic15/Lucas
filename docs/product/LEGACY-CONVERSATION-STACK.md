# The legacy conversation stack — what it is, and how to put an avatar back on it

*Written 2026-07-26, when Laura (`avatars/petra/`) moved to the ElevenLabs Agents
runtime and Cedric was already there. This is the preserved recipe for the stack
they left behind: the one we built ourselves, tuned for months, and can return to
in one commit.*

Nothing here is deleted by that move. Every knob still parses, every provider is
still wired, every test still passes. What changes is only **which of the two
runtimes a given avatar is dispatched onto** — and that is four lines of config.

---

## What the legacy stack actually is

A pipeline we assembled from parts we chose individually, each for a reason:

| Stage | What runs | Why this one |
|---|---|---|
| Speech → text | **Deepgram `nova-3`**, `multi` language, streaming | Fastest reliable multilingual streaming ASR; `DEEPGRAM_ENDPOINTING_MS` is what we tune the turn feel against |
| Live spoken reply | **Cerebras `gemma-4-31b`** | ~0.17s to first token. On the live path latency *is* the product — this is the whole reason the legacy stack exists |
| Deep-thought escalation | **Anthropic** `claude-haiku-4-5` (`BRAIN_MODEL_COMPLEX`) | Only for questions worth the extra second |
| Post-meeting artifacts | **Anthropic `claude-opus-4-8`** (`BRAIN_PROVIDER_POST=anthropic`) | Quality over speed once nobody is waiting |
| Web search on the live path | `LIVE_SEARCH_MODEL` | Announced out loud, then always delivered |
| Retrieval | **fastembed**, local (`EMBEDDING_PROVIDER=local`) | No vendor, no key, no per-query cost |
| Cross-meeting memory | **Graphiti** on Neo4j Aura | Temporal knowledge graph, extraction on our own stack |
| Voice out | **ElevenLabs TTS** (`eleven_flash_v2_5`) with per-word timings | The timings are what drive real lip-sync on `/talk` |

The wire format is OpenAI-compatible, so Cerebras is reached through the `groq`
provider with `GROQ_BASE=https://api.cerebras.ai/v1` — a historical accident worth
knowing before you go hunting for a "cerebras" provider in the env.

### The part that is genuinely ours: multiparty behaviour

This is the piece with no equivalent on the agent runtime, and the reason this
document exists. All of it lives **below the early return for agent-runtime
sessions** in `backend/app/main.py` — so an avatar on ElevenLabs still carries
these settings in its yaml and no longer obeys any of them:

- **`require_wake_word: true`** — with ≥2 humans she speaks only when named; in a
  1:1 she is fluid and needs no name. (`_wake_required()`)
- **The 15-second follow-up window** — once she is talking with you, your next
  sentence doesn't need her name again. This is the single most conversational
  behaviour in the stack, and the agent runtime has no equivalent: its gate
  closes on `agent_response_complete`, so **every** turn re-requires the name.
- **Adaptive deference** — `DEFERENCE_MIN_SECONDS` / `DEFERENCE_ACTIVE_PARTIAL_SECONDS`:
  she waits out a human who is mid-sentence instead of talking over them.
- **`speak_cooldown_seconds`** — the floor between two of her turns.
- **Opening grace** (`OPENING_GRACE_SECONDS`) — she doesn't pounce on the first
  seconds of a call while people are still saying hello.
- **Hand-raise etiquette** — she signals she has something and waits to be invited.
- **Addressed-to-other suppression**, joiner greetings, backchannel handling, the
  quiet-participant nudge, the proactive closing intervention.

Everything else in that list (deference, grace, endpointing) is **global env**,
shared by every avatar — only `require_wake_word` and `speak_cooldown_seconds`
are per-avatar in `avatar.yaml`.

---

## Putting an avatar back on it

Fully reversible, no data migration, no external service to undo:

1. **In `avatars/<avatar>/avatar.yaml`** — delete these two lines (absent = legacy;
   the dispatcher is fail-closed, so removing either one is enough):
   ```yaml
   conversation_runtime: elevenlabs_agent
   elevenlabs_agent_id: "agent_…"
   ```
   Keep `face: talk`. Confirm the legacy behaviour knobs are still there —
   `require_wake_word: true`, `speak_cooldown_seconds: 5`.

2. **On App Runner** — remove the avatar id from `ELEVENLABS_AGENT_AVATAR_ALLOWLIST`
   (or unset it entirely: the code default is `cedric` only). Remember
   `update-service` **replaces** the whole env map — merge the full map, never
   send a partial one.

3. **Nothing else.** The Cerebras/Deepgram/Anthropic path was never turned off:
   `BRAIN_PROVIDER`, `GROQ_BASE`, `BRAIN_MODEL_FAST`, `RECALL_TRANSCRIPTION_PROVIDER`
   are untouched by the runtime switch, because avatars on the agent runtime still
   use Deepgram — the multiparty gate needs its transcript to detect the wake word.

Either change alone reverts the avatar. Both are one commit.

### Kill switch for everyone at once

`ELEVENLABS_AGENT_RUNTIME_ENABLED=false` puts **every** avatar back on the legacy
pipeline immediately, without touching any yaml. That is the panic button.

---

## What you lose by leaving, in one line each

- The 15-second follow-up window → every turn needs her name again.
- In a **1:1**, `require_wake_word` is silently dropped: the ElevenLabs gate only
  engages at ≥2 humans, so below that all room audio reaches the agent and the
  only discipline is a prompt line.
- The capability-honesty work (typed vs untyped actions, "never say done",
  clarify-vs-search, knowledge routing) survives **only** as prompt text in the
  agent config — it is no longer enforced by code.
- `about/` self-knowledge and Graphiti cross-meeting carryover are not wired into
  `api/voice_agent.py`: on the agent runtime she cannot answer "how were you
  built?" from her own docs, and does not carry context between meetings.
- Per-conversation-minute ElevenLabs billing, stacked on Recall **and** Deepgram.

## What you gain

- Turn-taking and interruption handling that were bought, not built — measurably
  better in a 3-person room, because human↔human audio never even reaches the
  model.
- Colocated inference (no external LLM hop) and one vendor owning the whole
  speech loop.

---

## Related

- `docs/product/CEDRIC-ELEVENLABS-PILOT.md` — the agent-runtime design, its
  acceptance gates, and the per-knob "why" for the agent's own config.
- `backend/scripts/create_meeting_agent.py` — the agent config as code. The
  repo, not the ElevenLabs dashboard, is the source of truth.
- `backend/app/integrations/elevenlabs_agent.py` — the fail-closed dispatcher.
