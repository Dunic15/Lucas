# Codex task brief — Callable AI Process Avatars

You are working **in parallel with another agent (Claude Code)** on this repo.
Read [README.md](README.md) first for what this project is and how it fits
together, and [avatars/README.md](avatars/README.md) for the avatar format.

To avoid collisions, **work on a branch and open a PR** — do not commit to
`main`:

```bash
git checkout -b codex/content-and-ui
# ...do the tasks...
git push -u origin codex/content-and-ui
```

---

## ⚠️ File ownership (do not cross)

Claude is actively editing the backend right now. **Do NOT touch any of these:**

- `backend/**` (all Python, tests, scripts)
- `requirements.txt`
- `.env.example`
- `README.md`  (Claude will add links to your new files)

**You own exactly these areas** (nothing here overlaps Claude's work):

- `avatars/marcus/**`   — a new second avatar (new files only)
- `frontend/avatar.html` — UI polish
- `docs/**`             — new docs

If you think you need to edit a backend file, **stop and leave a note in your
PR description instead** — don't edit it.

---

## Task 1 — Author a second avatar: "Marcus — AI IT/Security Expert"

Goal: prove the "add an avatar = add a folder" design with a real second avatar.
Copy the shape of `avatars/sofia/`.

Create:
- `avatars/marcus/avatar.yaml` — `id: marcus`, `name: Marcus`,
  `role: AI IT & Security Expert`, `wake_words: [marcus]`, a `persona_prompt`
  about access provisioning, SSO, security reviews, and offboarding. Leave
  `tavus_replica_id` / `elevenlabs_voice_id` / `min_confidence` /
  `speak_cooldown_seconds` blank (they fall back to global `.env`).
- `avatars/marcus/knowledge/` with **2–3 synthetic markdown SOPs**, e.g.
  `access_provisioning_sop.md`, `offboarding_security_sop.md`,
  `incident_response_sop.md`.

Match the existing knowledge-doc structure (headings like *Required steps*,
*Approvals required*, *Owners*, *Definition of done*, *Common gaps*). Headings
become retrieval sections and get cited, so keep one concept per heading.

**Synthetic data only** — no real people, customers, or company names.

Acceptance: the folder follows the same shape as `avatars/sofia/`; YAML is
valid; `id` equals the folder name `marcus`.

---

## Task 2 — Polish the avatar page (`frontend/avatar.html`)

This page is what the Recall.ai bot renders as its camera. Improve the UX
**without breaking the integration contract below.**

Add:
1. A **connection-status pill** (e.g. "Connecting…" → "Live") reflecting the
   websocket state.
2. A **caption bar** that shows the latest line the avatar said (the `text`
   from each `speak` message), so viewers can read along.
3. A subtle **"speaking" state** on the badge (e.g. the dot pulses while a line
   is being spoken).
4. A clean state when `conversation_url` / `conversation_id` params are missing.

### Integration contract — DO NOT CHANGE these mechanics
- The page connects to `ws://<host>/ws/<conversation_id>`.
- The backend sends JSON messages: `{ "type": "speak", "text": "<words>" }`.
- On each `speak` message the page MUST still call `speak(text)`, which sends
  the Tavus **echo** app-message into the Daily room (that's what makes the
  avatar talk). You may *additionally* render `text` as a caption.
- Keep the pinned Daily JS `<script>` tag and the Daily iframe embed.

Acceptance: open
`avatar.html?conversation_url=https://x&conversation_id=abc` in a browser — it
renders, shows "Connecting…", and degrades gracefully (no console errors) when
the websocket can't connect. Add a 2-line comment explaining how to manually
test a caption (e.g. paste a fake `onmessage` call in devtools).

---

## Task 3 — Docs: `docs/FREE_TIER.md` and `docs/DEMO.md`

### `docs/FREE_TIER.md` — "can I build/test this for free?"
A table of every external service with: free/trial/paid, rough limits, signup
link, and what it's needed for. Verify current limits before publishing (they
change). Use these starting facts and confirm/refine:

| Service | Role | Free? (verify) |
|---|---|---|
| Ollama (local LLM) | the brain, free path | Free, runs locally |
| Local embeddings (e.g. fastembed / sentence-transformers) | RAG, free path | Free, runs locally |
| Anthropic Claude | the brain, best quality | Paid per-token (no standing free tier) |
| Voyage AI | embeddings, best quality | Free tier exists — verify token limit |
| Recall.ai | meeting entry (ears + camera) | Paid (~$0.50/hr); trial credits — verify |
| Tavus | avatar face | Free/trial minutes — verify |
| ElevenLabs | voice | Free tier (monthly credits) — verify |

End with the recommendation: **the brain + logic run 100% free** on Ollama +
local embeddings + the offline simulator (Claude is adding this); only the
**live meeting demo** (Recall + Tavus + ElevenLabs) needs trials/paid, and only
for the final "face in a Zoom" step.

### `docs/DEMO.md` — demo runbook
Two paths:
1. **Free / offline** (no keys): build the index, run the simulator against a
   sample transcript, show the grounded answer + post-meeting checklist.
   (Claude is adding the simulator + an `ask` CLI under `backend/`; reference
   them by the commands the README will document — check the README after
   Claude pushes, or note them as TODO if not present yet.)
2. **Live** (trials): ngrok + `POST /sessions/start` against a test Google Meet,
   say the wake word, end the session, show the artifact.

---

## Conventions
- Synthetic, audit-safe data only. **No secrets, no real PII.**
- Match the existing tone and markdown/code style.
- Small, focused commits with clear messages.
- When done, push your branch and open a PR titled
  `Codex: second avatar (Marcus) + avatar page polish + free-tier/demo docs`,
  listing what you changed and anything you wanted to touch in `backend/` but
  didn't.
