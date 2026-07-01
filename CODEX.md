# Codex task brief — Callable AI Process Avatars

> **Status: no open Codex tasks.** All three items below were completed by Claude
> Code and merged to `main` (Anam face-vendor swap + avatar-page polish; the
> second avatar "Marcus" was descoped by the owner). This file is kept for
> reference / future parallel work. Nothing here needs redoing — do not reopen
> these tasks or you'll conflict with `main`.

You are working **in parallel with Claude Code** on this repo. Read
[README.md](README.md) first (especially the demo), then
[avatars/README.md](avatars/README.md) for the avatar format.

## Claude handoff note — Laura rename

Codex renamed the default avatar identity to Laura across README, avatar config,
defaults, scripts, tests, and Claude agent instructions. This is
not a demo behavior change: the offline demo still starts with the same command,
still uses free stub/hash mode without keys, and still exercises `/demo/ask`,
`/demo/sample`, and `/demo/post_meeting`; the default `avatar_id` and wake word
are now `laura`.

Work on a branch and open a PR — **do not commit to `main`:**

```bash
git checkout -b codex/content-and-face
# ...do the tasks...
git push -u origin codex/content-and-face
```

---

## ⚠️ File ownership (do not cross)

Claude has just landed the **offline demo** (brain providers, RAG auto-index,
`/demo/*` routes, `frontend/demo.html`, docs, `.claude/agents`). **Do NOT touch:**

- `backend/app/{main.py,brain.py,llm.py,embeddings.py,rag.py,config.py,decision.py,store.py,avatars.py,recall_client.py}`
- `backend/scripts/**`, `backend/tests/**`
- `frontend/demo.html`
- `requirements.txt`, `.env.example`, `README.md`, `QUICKSTART.md`, `docs/**`, `.claude/**`

**You own exactly these:**

- `backend/app/tavus_client.py` — the FACE-vendor client (see Task 3, Anam swap)
- `avatars/marcus/**` — a new second avatar (new files only)
- `frontend/avatar.html` — the live-meeting avatar page (UI polish)

If you must change any other backend file (e.g. the `tavus_client` import line in
`main.py` for the rename), **leave a note in the PR description** and Claude will
apply that one line — don't edit `main.py` yourself.

---

## Task 1 — Second avatar: "Marcus — AI IT/Security Expert"

Prove "add an avatar = add a folder." Copy the shape of `avatars/laura/`.

Create:
- `avatars/marcus/avatar.yaml` — `id: marcus`, `name: Marcus`,
  `role: AI IT & Security Expert`, `wake_words: [marcus]`, a `persona_prompt`
  about access provisioning, SSO, security reviews, and offboarding. Leave the
  face/voice/threshold fields blank (they fall back to global `.env`).
- `avatars/marcus/knowledge/` — 2–3 **synthetic** SOPs
  (`access_provisioning_sop.md`, `offboarding_security_sop.md`,
  `incident_response_sop.md`). Match Laura's heading structure — headings become
  cited retrieval sections, so keep one concept per heading.
- `avatars/marcus/sample_meeting.txt` — a short transcript with a couple of gaps.

**Synthetic data only.** Acceptance: same shape as `avatars/laura/`; valid YAML;
`id` equals the folder name. Verify: `python backend/scripts/ask.py --avatar marcus "<question>"`.

## Task 2 — Polish the live avatar page (`frontend/avatar.html`)

This is what the Recall bot renders as its camera. Improve UX **without breaking
the integration contract**:
- connection-status pill (Connecting… → Live), a caption bar showing the last
  spoken line, a subtle "speaking" pulse, and a clean state when params missing.
- **Do NOT change:** the `ws://<host>/ws/<conversation_id>` connection, the
  `{type:"speak", text}` message handling, the `speak()` echo app-message, or the
  pinned face-SDK `<script>` + iframe embed.

## Task 3 — Face vendor: finish the Tavus → **Anam** swap (`tavus_client.py` only)

The face vendor is Anam now. In **`backend/app/tavus_client.py` only**:
- Update the docstring/comments and the vendor calls to Anam's API while keeping
  the **exact same function signatures** (`create_persona`, `create_conversation`,
  `end_conversation`) so `main.py` needs no change beyond the import name.
- In the PR description, give Claude the one line to change in `main.py`
  (`from . import ... tavus_client` → your new module name) if you rename the file.
- Update `frontend/avatar.html` comments to say Anam.

Keep `config.py` env var names as they are unless you note the change for Claude.

## Conventions
- Synthetic, audit-safe data only. No secrets, no real PII.
- Match existing tone and style. Small, focused commits.
- PR title: `Codex: Marcus avatar + avatar page polish + Anam face client`.
  List what you changed and any one-line backend edits you want Claude to apply.
