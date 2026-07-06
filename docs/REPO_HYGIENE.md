# Repo hygiene — current vs. stale (updated 2026-07-06, declutter pass)

First inventoried after the MeetingState + GPU cost-control work landed on `main`;
updated by the **declutter pass** (branch `claude/repo-declutter`), which executed
the approved moves. **Nothing has been deleted** — deletion candidates still need
explicit owner approval. Owner: Claude 1 (docs/repo hygiene session).

## Files kept in root (the essentials, nothing else)

| Path | Why it must stay at root |
|---|---|
| `README.md` | the front door |
| `CODEX.md` | parallel-work brief + integration contract (linked from README/CONTEXT) |
| `CLAUDE.md` | Claude Code reads it from repo root by convention |
| `requirements.txt` | `pip install -r` quickstart path |
| `.env.example` | `cp .env.example .env` quickstart path |
| `.gitignore`, `.mcp.json` | git / Claude Code tooling — root-required |

(Local untracked `.env*` files also live at root but are gitignored — invisible
on GitHub, never committed, not touched by the declutter.)

## Files moved (declutter pass, all via `git mv` — history preserved)

| From (repo root) | To |
|---|---|
| 17 lookdev screenshots (`avatarsdk-hd.jpeg`, `avaturn-hd.jpeg`, `deployed-office-look.jpeg`, `final-frame-check.jpeg`, `final-meeting-view.jpeg`, `laura-hd-speaking.jpeg`, `lookdev-final.jpeg`, `lookdev-office.jpeg`, `meeting-final.jpeg`, `meeting-look-v1.jpeg`, `meeting-look-v2.jpeg`, `talk-avatar-live.jpeg`, `talk-avatar-test.jpeg`, `tune-v1-head.jpeg`, `tune-v2-upper-close.jpeg`, `tune-v3.jpeg`, `tune-v5-direct.jpeg`) | `docs/assets/lookdev/` |
| `photoreal-e2e-proof.png` (was untracked) | `docs/assets/proofs/` (now tracked) |

## Files archived (moved to `docs/archive/`, each with an "Archived" banner)

| File | Why archived |
|---|---|
| `QUICKSTART.md` | duplicated the README quickstart; README is canonical |
| `CLAUDE_CODE_STARTUP_AGENTS_PROMPT.md` | one-off session-bootstrap prompt, not product |
| `render.yaml` | Render suspended; blueprint file kept for the fallback trail. **Known accepted risk:** Render Blueprints conventionally expect `render.yaml` at repo root — if the suspended service is ever resumed via Blueprint-sync, move it back first (reversible `git mv`) |
| `Dockerfile` | generic container deploy; App Runner is source-based — unused |
| `docs/DEPLOY.md` | claimed Render-primary + Claude-Haiku-live; both false (App Runner primary, Groq live). A fresh DEPLOY doc is a future todo for backend-infra |
| `docs/AWS_MIGRATION_ASSESSMENT.md` | pre-migration decision doc; migration done 2026-07-03 — "why" trail only |
| `docs/LATENCY_OPTIMIZATION.md` | Haiku-era analysis; superseded by the Groq switch |

Cross-references updated: `.claude/agents/backend-infra.md` and
`.claude/agents/finance-unit-economics.md` now point at the `docs/archive/` paths
(and backend-infra reads `gpu/README.md` instead, which is current).

## Still recommended for deletion later (needs explicit owner approval)

| Path | Why |
|---|---|
| most of `docs/assets/lookdev/*` | iteration artifacts; keep 2–3 representative shots, drop the rest (they inflate clone size) |
| `docs/archive/QUICKSTART.md` | zero unique content vs. README; archived only pending approval to delete |
| remote branches: `claude/repo-hygiene`, `codex/eval-suite`, `claude/demo-readiness-ui` | merged into main (PRs #5/#4/#6) — safe to delete |
| remote branch `optimize-cscs-latency-hop-metrics` | unmerged but fully superseded (47 behind / 1 ahead; Anam/Render-era diff) |
| remote branch `codex/content-and-ui` + **PR #1** | superseded — PR #1 should be **closed without merging** (its commits re-introduce Anam-primary content; `CODEX.md` History already says don't reopen) |

## Not touched, and why

| Path | Why untouched |
|---|---|
| `backend/**`, `frontend/**`, `gpu/**`, `avatars/**`, `tests/**`, `scripts/` | product/runtime/tests — behavior changes are out of scope for a declutter |
| `extensions/laura-meet/`, `lovable/laura-meeting-expert/` | working entry point / marketing-site source |
| `docs/CALENDAR.md`, `docs/DEMO.md`, `docs/FREE_TIER.md` | mostly accurate setup notes — current, not clutter |
| `docs/research/`, `docs/product/`, `docs/gtm/`, `docs/fundraise/` | current agent-team output directories |
| local `.env`, `.env.filled`, `.env.qa-backup-20260706` | untracked + gitignored, may hold live keys — never git-managed |
| `backend/app/decision.py` docstring | still says "speaks ONLY when called by name" (false since the SKIP-gate rewrite) — a **code** change for the backend owner, not a docs pass |

## Known doc-vs-code drift accepted for now

- `AVATAR_PAGE` **code default is `"avatar"`** (Anam) while production runs `talk`.
  Changing the default is a product-behavior change — flagged for the owner.
- `RECALL_API_BASE` code default is `us-west-2` while everything real uses
  `eu-central-1` (same reasoning).
- `MIN_CONFIDENCE` / `decision.passes_confidence()` is dead on the live streaming
  path (superseded by the in-stream SKIP gate) — candidate for removal by the
  backend owner.
- `.env.example` doesn't mention `AVATAR_PAGE` or the `GPU_*` vars — worth adding
  next time the backend owner touches env plumbing.
