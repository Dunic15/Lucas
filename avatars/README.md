# Avatar

This MVP ships **one callable AI agent**: Sofia. Keep the repo focused on this
first agent until the live meeting loop is proven.

```
avatars/
  sofia/
    avatar.yaml          ← the knobs (name, wake words, persona, face, voice)
    knowledge/           ← the process docs the avatar answers from
      onboarding_sop.md
      access_security_sop.md
```

You do **not** need to touch Python to change Sofia's behaviour or knowledge.
Edit `avatar.yaml`, add markdown to `knowledge/`, then rebuild the index:

```bash
python backend/scripts/ingest.py
```

## Field reference (`avatar.yaml`)

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Must equal the folder name. Used as `avatar_id` in the API. |
| `name` | yes | Display/spoken name. |
| `role` | yes | Short description of expertise. |
| `wake_words` | yes | List of names that call the avatar to speak. |
| `persona_prompt` | yes | How the avatar introduces itself; injected into the system prompt. |
| `anam_avatar_id` | no | Per-avatar Anam face. Blank -> global `ANAM_AVATAR_ID`. |
| `anam_avatar_model` | no | Per-avatar Anam model. Blank -> global `ANAM_AVATAR_MODEL`. |
| `anam_voice_id` | no | Per-avatar Anam voice. Blank -> global `ANAM_VOICE_ID`. |
| `min_confidence` | no | Speak threshold 0–1. Blank → global `MIN_CONFIDENCE`. |
| `speak_cooldown_seconds` | no | Quiet time after speaking. Blank → global default. |

## Knowledge docs

Plain markdown. Headings become retrieval sections and are cited back to the
team (e.g. *"per onboarding_sop.md — Approvals required"*). Keep one process
concept per heading so citations stay precise. **No real PII** in committed
docs — use synthetic examples.
