# Repo hygiene — current state (updated 2026-07-14)

Living inventory of what's canonical vs. stale. Last full pass: **2026-07-14
doc-consolidation + branch-realignment** (branch `claude/repo-hygiene-2026-07-14`,
cut fresh from `origin/main`). Prior passes: 2026-07-06 declutter, 2026-07-07
repo-alignment (history at the bottom).

## Single sources of truth (start here)

| Question | Canonical doc |
|---|---|
| What is Laura / the front door | [`../README.md`](../README.md) |
| How it works today (architecture + seams) | [`ARCHITECTURE_CURRENT.md`](ARCHITECTURE_CURRENT.md) |
| Why it wins (positioning) | [`product/WEDGE.md`](product/WEDGE.md) |
| What we're building — Now/Next/Later | [`product/roadmap.md`](product/roadmap.md) |
| Latest production-hardening handoff | [`product/SESSION-HANDOFF-2026-07-13-audit.md`](product/SESSION-HANDOFF-2026-07-13-audit.md) |
| Flow-by-flow audit (repro + fixes) | [`product/FLOW-AUDIT-2026-07-13.md`](product/FLOW-AUDIT-2026-07-13.md) |
| Parallel-session integration contract | [`../CODEX.md`](../CODEX.md) |

## The 2026-07-14 pass — what it did

**Branch realignment (important).** The local working branch `claude/dash-detail`
had **fully diverged history from `origin/main`** — no common ancestor (`git
merge-base` empty). `origin/main` had been rewritten/squashed (its root is a recent
commit) and is the **authoritative, fresh, CI-green line** (memory + verified: last
commit hours old, contains #199–#204: native executor, per-meeting mission, Approve
button, Gemini-via-Vertex brain). A normal merge/rebase was impossible.

Resolution (owner-approved): cut a **fresh branch from `origin/main`**
(`claude/repo-hygiene-2026-07-14`), carry over only the genuinely-new local docs,
and **keep `claude/dash-detail` intact as a backup** — it still holds two
local-only features not on `origin/main` (a *private-beta login gate* and the
*excel-filter dashboard*) that can be re-PR'd from it if wanted. A full tarball of
every untracked+modified file from the old branch is in the session scratchpad.

**Junk deleted:**
- 28 identical `X 2.ext` sync-collision duplicates (Drive/iCloud artifacts) — byte-for-byte copies of tracked files.
- 20 untracked copies of files `origin/main` already tracks (took origin's canonical version).
- stray root `SKILL.md` (an accidental copy of `.claude/skills/deploy-on-shared-vercel/SKILL.md`).

**Docs consolidated:** the freshest roadmap (`ROADMAP-2026-07-14.md`) is now the
canonical [`product/roadmap.md`](product/roadmap.md); ~11 overlapping
roadmap/business-plan/handoff snapshots moved to
[`archive/2026-07-14/`](archive/2026-07-14/) (banner + index there). Active plans
(native integrations, photoreal-Ditto, connections, YC niche research) stay live in
`product/`.

**Gitignore hardened:** local A/V test dirs (`avatar-test-clips/`,
`desktop-laura-stuff/`, `voice-previews/`, `images laura/`) and GTM lead lists with
real prospect emails (`docs/gtm/*.xlsx`, `docs/gtm/recipients.csv` — PII) are now
ignored. GTM *strategy* `.md` docs stay tracked; the raw contact data never enters git.

**Not done (blocked):** re-adding `supabase` + `stripe` MCP servers to `.mcp.json`
was blocked by the self-modification guard (adding a full-access Stripe server is
out of scope for a hygiene task). The local variant is preserved in the scratchpad
backup — re-apply deliberately if you want those MCP servers.

## Files kept at root (essentials only)

`README.md` · `CODEX.md` · `CLAUDE.md` · `requirements.txt` · `.env.example` ·
`.gitignore` · `.mcp.json`. Local untracked `.env*` stay gitignored (may hold live
keys) — never git-managed.

## Follow-ups / known residue (owner decision)

| Item | Note |
|---|---|
| `claude/dash-detail` local-only features | private-beta login gate + excel-filter dashboard live only on that backup branch — re-PR onto `origin/main` if still wanted (its dashboard is newer, expect conflicts). |
| Stale remote branches | many merged `claude/*` / `codex/*` PR branches remain on the remote — prune with `gh` when convenient. |
| `.claude/worktrees/` + `git worktree list` | several prunable/locked worktrees from parallel sessions — `git worktree prune` when no session is mid-flight. |
| `docs/assets/lookdev/*` | avatar-iteration screenshots — keep 2–3, drop the rest (clone-size). |

## History (resolved earlier)

- **2026-07-07 repo-alignment:** `AVATAR_PAGE`→`talk`, `RECALL_API_BASE`→`eu-central-1`,
  `MIN_CONFIDENCE` labelled LEGACY, `decision.py` docstring corrected, `.env.example`
  documents `AVATAR_PAGE`/`GROQ_*`/`GPU_*`. PR #1 (Anam-first) closed without merging.
- **2026-07-06 declutter:** 17 lookdev screenshots → `docs/assets/lookdev/`; stale
  root docs (`QUICKSTART.md`, `render.yaml`, `Dockerfile`, `DEPLOY.md`,
  `AWS_MIGRATION_ASSESSMENT.md`, `LATENCY_OPTIMIZATION.md`) → `docs/archive/`.
