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

**Every avatar also gets its own email, for free.** The watched inbox
(`CALENDAR_INVITE_EMAILS`) answers to plus-aliases: invite
`laura.ai.122222+marcus@gmail.com` to a calendar event — or add it via Meet's
"Add people" — and *marcus* joins instead of the default avatar. Bare address
or unknown tag → `DEFAULT_AVATAR_ID`.

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
| `drive_folder_id` | no | Google Drive folder read at session start (drive_client): its docs become part of the avatar's pre-meeting brief. Needs the `drive.readonly` scope on the connected Google account. |
| `min_confidence` | no | Speak threshold 0–1. Blank → global `MIN_CONFIDENCE`. |
| `speak_cooldown_seconds` | no | Quiet time after speaking. Blank → global default. |

## The 3D face (`frontend/<id>.glb`)

Check any new model BEFORE a meeting — a face that loads fine in a viewer can
still be broken in a call, and the symptom (head frozen pitched down, dead
lipsync) only shows up live:

```bash
python3 backend/scripts/check_avatar_model.py frontend/<id>.glb   # 0 = usable
```

What a usable model needs:

- **All 15 Oculus visemes** (`viseme_sil/PP/FF/TH/DD/kk/CH/SS/nn/RR/aa/E/I/O/U`)
  — this is what moves the mouth. Note the vowels are `I`/`O`/`U`.
- **`eyesLookUp` + `eyesLookDown` aggregates**, OR a rig whose head node is named
  `AvatarHead` (talk.html patches that case). Per-eye `eyeLook*` morphs *without*
  the aggregates and *without* `AvatarHead` makes TalkingHead's `animate()` throw
  every frame — blink, pose and lipsync all die.
- **~25–50k triangles.** `cedric.glb` is 48.7k, `laura.glb` 31.2k; `petra.glb` is
  13.3k (TalkingHead's stock `brunette.glb`) and visibly reads low-poly next to
  them. Poly count is the *look*; visemes are the *behaviour* — don't trade the
  second for the first.
- **~12–14 MB.** The bot browser downloads this on the meeting path.

Sources: Ready Player Me is **dead** (`models.readyplayer.me` = NXDOMAIN), so RPM
models can never be re-exported — what's vendored is all there is. Avaturn and
MetaPerson need a logged-in creator account (Avaturn exports via their Discord
bot). TalkingHead's repo samples are the free fallback, but only `avaturn.glb`
(=`laura.glb`) and `avatarsdk.glb` (=`cedric.glb`) are both high-poly and
well-rigged; `mpfb.glb` is high-poly but fails the eye-aggregate check above.

Identity rule: a missing asset is surfaced as unavailable, **never** borrowed from
another avatar — two avatars must never share a face.

## Knowledge docs

Plain markdown. Headings become retrieval sections and are cited back to the
team (e.g. *"per onboarding_sop.md — Approvals required"*). Keep one process
concept per heading so citations stay precise. **No real PII** in committed
docs — use synthetic examples.
