# Codex brief: Callable AI Process Avatars

You are working **in parallel with two Claude Code sessions** on this repo.
Read [`.claude/CONTEXT.md`](.claude/CONTEXT.md) first (product truth), then
[README.md](README.md). Session ownership lives in
[`.claude/agents/README.md`](.claude/agents/README.md):
**Claude 1 = docs/repo hygiene · Claude 2 = demo UI/product surface · Codex = tests/evals.**

## Your scope (current)

**Codex owns tests and evals ONLY, for now:**

- `backend/tests/**`: the unit/logic test suite (pure logic, no keys needed).
- `tests/**`: the eval harness (e.g. `tests/eval_laura_onboarding.py`,
  `tests/test_laura_onboarding_eval.py`, `tests/fixtures/`): scenario transcripts →
  expected MeetingState/artifact outcomes.

**Out of scope unless explicitly asked:**

- **No backend runtime changes** (`backend/app/**`): if a test exposes a bug,
  write the failing test + a note in the PR; the owning Claude session fixes it.
- **No GPU work** (`gpu/**`, `backend/app/gpu_runtime.py`) and **no MeetingState
  implementation changes** (`backend/app/meeting_state.py`,
  `avatars/*/process_templates/`); testing them is in scope, changing them is not.
- No `frontend/**`, no `docs/**`, no `.claude/**`, no `README.md`.

## Rules

1. **Branch required: never commit to `main`:**

   ```bash
   git checkout -b codex/<topic>
   # ...work...
   git push -u origin codex/<topic>   # then open a PR
   ```

   Every PR gets a code-reviewer pass before merge.

2. **Synthetic, audit-safe data only**: test fixtures and eval transcripts must
   contain no real PII or customer names.

3. Tests must run key-free (`stub` brain + `hash` embeddings), like the demo.

4. Match existing tone and style. Small, focused commits.

## Live-meeting integration CONTRACT (never break, and test against it)

This is the contract the whole team codes against; hard constraint 1 in
`.claude/CONTEXT.md`:

- The avatar page speak channel: `ws://<host>/ws/<conversation_id>` **and** its
  App-Runner-safe twins: SSE `GET /avatar/stream/<conversation_id>` + HTTP poll
  `GET /avatar/messages/<conversation_id>`. A message is routed down exactly one
  path (ws if connected, else queued for SSE/poll) so pages never double-speak.
- The message shape: `{"type": "speak", "text": "..."}`.
- On the legacy Anam page (`frontend/avatar.html`): the `speak()` echo entry
  point and the **pinned** face-SDK embed (`@anam-ai/js-sdk@4.17.1`, pinned to
  limit supply-chain surface; do not unpin or refactor away).
- The `recall_client` / `anam_client` function signatures
  (`create_persona`, `create_conversation`, `end_conversation`, `create_bot`,
  `leave_call`).
- The GPU stream protocol (photoreal): client sends
  `{"type":"speak","audio_b64":<mp3>}`; server replies `hello`/`talk_start`/
  `talk_end` JSON frames + binary JPEG frames (`gpu/README.md`).

## History

Earlier Codex tasks (Marcus avatar, avatar-page polish, Tavus→Anam swap) are done
or descoped; merged via `main` long ago; the old `codex/content-and-ui` branch is
stale and superseded. Don't reopen them.
