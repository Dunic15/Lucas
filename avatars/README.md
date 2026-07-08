# Avatars

Each folder here is **one callable avatar**. An avatar is just:

```
avatars/
  laura/
    avatar.yaml          ← the knobs (name, wake words, persona, face, voice)
    knowledge/           ← the process docs the avatar answers from
      onboarding_sop.md
      access_security_sop.md
```

You do **not** need to touch any Python to add or change an avatar.

## Add a new avatar in 3 steps

1. **Copy the folder.** Duplicate `avatars/laura/` and rename it, e.g.
   `avatars/marcus/` (for an "AI IT/Security Expert").
2. **Edit `avatar.yaml`.** Set `id` (must match the folder name), `name`,
   `role`, `wake_words`, and the `persona_prompt`. Optionally give it its own
   Anam face and ElevenLabs voice; leave those blank to use the global `.env`.
3. **Add its knowledge.** Drop the relevant `.md` process docs into the new
   `knowledge/` folder, then build the index:
   ```bash
   python backend/scripts/ingest.py        # indexes every avatar
   ```

Call it into a meeting by passing its id:
```bash
curl -X POST http://127.0.0.1:8000/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"meeting_url": "...", "avatar_id": "marcus"}'
```
(`avatar_id` defaults to `laura` if omitted.)

## Field reference (`avatar.yaml`)

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Must equal the folder name. Used as `avatar_id` in the API. |
| `name` | yes | Display/spoken name. |
| `role` | yes | Short description of expertise. |
| `wake_words` | yes | List of names that call the avatar to speak. |
| `persona_prompt` | yes | How the avatar introduces itself; injected into the system prompt. |
| `anam_avatar_id` | no | Per-avatar face. Blank → global `ANAM_AVATAR_ID`. |
| `elevenlabs_voice_id` | no | Per-avatar voice. Blank → global `ELEVENLABS_VOICE_ID`. |
| `knowledge_packs` | no | List of OTHER avatar ids whose `knowledge/` this avatar also retrieves from (e.g. cedric reuses the `sff` pack) — packs live in one place, never copied. |
| `talk_body` | no | `F` (default) or `M` — TalkingHead pose/gesture set on the /talk renderer. The 3D model is `frontend/<id>.glb`, falling back to `laura.glb`. |
| `min_confidence` | no | Speak threshold 0–1. Blank → global `MIN_CONFIDENCE`. |
| `speak_cooldown_seconds` | no | Quiet time after speaking. Blank → global default. |

## Knowledge docs

Plain markdown. Headings become retrieval sections and are cited back to the
team (e.g. *"per onboarding_sop.md — Approvals required"*). Keep one process
concept per heading so citations stay precise. **No real PII** in committed
docs — use synthetic examples.
