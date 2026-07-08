---
description: Review the working diff against Laura's contract/latency/PII/meter checklist
---

Review the current working diff (`git diff` + `git diff --cached`; if both are
empty, review the last commit `git show HEAD`) against this repo's non-negotiables,
in priority order:

1. **Live-meeting contract** — does anything change `ws/<conversation_id>`, the
   `{type:"speak",text}` message shape, the HTTP polling fallback
   (`/avatar/messages/<conversation_id>`), or the `recall_client` / `anam_client`
   function signatures? Any change here breaks running meetings.
2. **Latency hot path** — `answer_question_stream`, `/live/ask`, `/tts`, the
   webhook→speak dispatch: no added blocking calls, no sync I/O, no model swaps
   to slower providers. Latency is the product on the live path.
3. **PII** — transcripts must never be logged or persisted beyond session memory
   (no `print`/`logger` on transcript content).
4. **Meter safety** — every session start path must have a matching end path
   (`_finalize_session`); no code path may leave a Recall/Anam session running.
5. **Secrets** — nothing key-shaped in the diff; config only via env/SSM.
6. **Key-free demo** — `stub` + `hash` providers must still work with zero keys.
7. **Self-knowledge freshness** — if the diff changes Laura's architecture,
   vendors, tools, or meeting behaviour, `avatars/laura/about/` must be updated
   in the same diff (and the index rebuilt via `backend/scripts/ingest.py laura`).
   Laura answers "how were you built?" from those docs; a stale doc means she
   describes a system that no longer exists.

Report findings ranked most-severe first, each with file:line and a concrete
failure scenario. If the diff is clean, say so plainly.
