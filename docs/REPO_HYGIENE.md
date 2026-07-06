# Repo hygiene — current vs. stale (2026-07-06)

Inventory taken after the MeetingState + GPU cost-control work landed on `main`.
**Nothing has been deleted** — the "suggest" columns need owner approval first.
Owner: Claude 1 (docs/repo hygiene session).

## Keep — current and load-bearing

| Path | Why |
|---|---|
| `backend/app/**` | the product (incl. new `meeting_state.py`, `gpu_runtime.py`) |
| `backend/tests/**`, `tests/**` | unit suite + eval harness (Codex's domain) |
| `backend/scripts/**`, `scripts/` | offline pipeline (`ingest/ask/simulate`) + `latency_probe.py`, `serve.sh` |
| `gpu/**` | photoreal track: server, cost-control scripts, IAM policy, runbook |
| `frontend/talk.html`, `photoreal.html`, `demo.html`, `join.html`, `laura.glb` | live pages (talk = production default) |
| `frontend/avatar.html`, `live.html` | legacy Anam pages — keep while `/avatar` stays the documented fallback |
| `avatars/**` | the editable product surface (incl. `process_templates/`) |
| `.claude/**` | agents, hooks, commands, context — the operating system |
| `extensions/laura-meet/` | Chrome-extension entry point (still a documented entry path) |
| `backend/app/granola_client.py`, `backend/scripts/granola.py` | Granola post-meeting-only transcript source — optional (`GRANOLA_API_KEY`), functional, no Recall/Anam needed |
| `lovable/laura-meeting-expert/` | marketing-site source (lauravatar.com Cloudflare Worker) |
| `docs/research/meeting-agent-landscape.md` | current (2026-07-06) |
| `docs/CALENDAR.md`, `docs/DEMO.md`, `docs/FREE_TIER.md` | mostly accurate setup notes (spot-check on next touch) |

## Stale — contradict the current system (rewrite or archive)

| Path | What's wrong |
|---|---|
| `docs/DEPLOY.md` | presents **Render as the primary deploy** ("free tier, easiest") and says the live model is **Claude Haiku** — Render is suspended, App Runner is primary, live brain is **Groq llama-3.3-70b** (verified via live `/health`) |
| `docs/LATENCY_OPTIMIZATION.md` | Haiku-era latency analysis + "Render fallback configuration" — superseded by the Groq switch and AWS migration |
| `docs/AWS_MIGRATION_ASSESSMENT.md` | pre-migration decision doc; the migration happened (2026-07-03) — historical value only |
| `QUICKSTART.md` | duplicates the README quickstart; drifts every time README changes |
| `backend/app/decision.py` docstring | says "speaks ONLY when called by name" — false since the SKIP-gate rewrite (`require_wake_word=False`); fix next time that file is touched (code change — not this session) |

## Archive later (suggest `docs/archive/` — needs approval)

| Path | Note |
|---|---|
| `render.yaml` | Render is suspended; keep only if the "instant fallback" story stays, else archive |
| `Dockerfile` | generic container deploy; App Runner is source-based — unused today, harmless |
| `docs/AWS_MIGRATION_ASSESSMENT.md`, `docs/LATENCY_OPTIMIZATION.md` | historical records, useful for the "why" trail |
| `CLAUDE_CODE_STARTUP_AGENTS_PROMPT.md` (repo root) | one-off session-bootstrap prompt, not part of the product |

## Suggested deletions — DO NOT delete without explicit owner approval

| Path | Why it's clutter |
|---|---|
| **17 lookdev screenshots at repo root** — `avatarsdk-hd.jpeg`, `avaturn-hd.jpeg`, `deployed-office-look.jpeg`, `final-frame-check.jpeg`, `final-meeting-view.jpeg`, `laura-hd-speaking.jpeg`, `lookdev-final.jpeg`, `lookdev-office.jpeg`, `meeting-final.jpeg`, `meeting-look-v1.jpeg`, `meeting-look-v2.jpeg`, `talk-avatar-live.jpeg`, `talk-avatar-test.jpeg`, `tune-v1-head.jpeg`, `tune-v2-upper-close.jpeg`, `tune-v3.jpeg`, `tune-v5-direct.jpeg` | avatar look-dev iteration artifacts, all tracked in git; if any matter, move the keepers to `docs/lookdev/` and drop the rest |
| `photoreal-e2e-proof.png` (untracked) | session test artifact; screenshot preserved in issue/PR history |
| `QUICKSTART.md` | fold into README (already done in the rewrite) and delete |
| stale remote branches `codex/content-and-ui`, `optimize-cscs-latency-hop-metrics` | both superseded by `main` (verified 2026-07-06) |

## Known doc-vs-code drift accepted for now

- `AVATAR_PAGE` **code default is `"avatar"`** (Anam) while production runs `talk`.
  Changing the default is a product-behavior change — out of scope for a docs
  session; flagged for the owner.
- `RECALL_API_BASE` code default is `us-west-2` while everything real uses
  `eu-central-1` (same reasoning).
- `MIN_CONFIDENCE` / `decision.passes_confidence()` is dead on the live streaming
  path (superseded by the in-stream SKIP gate) — candidate for removal by the
  backend owner.
- `.env.example` doesn't mention `AVATAR_PAGE` or the `GPU_*` vars — worth adding
  next time the backend owner touches env plumbing.
