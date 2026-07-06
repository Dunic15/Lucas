# Laura — Agent Team & Operating Model

The "operating team" for the company. Every agent reads
[`.claude/CONTEXT.md`](../CONTEXT.md), `README.md`, and `CODEX.md` first. Claude Code
auto-routes by the `description` field; you can also name an agent explicitly.

## How we work (operating model — all sessions obey)

- **No direct pushes to `main`.** All work happens on a branch (or worktree) and
  merges through a PR: `claude/<topic>` for Claude sessions, `codex/<topic>` for
  Codex. Worktrees are preferred when parallel sessions share this checkout.
- **Every session declares, before touching files:** (1) which agent role it is
  acting as, and (2) exactly which files it will touch. Stay inside that set.
- **code-reviewer reviews every diff before merge** — contract, latency, PII,
  meter safety. No merge without it.
- **Session ownership (do not cross):**

  | Session | Owns |
  |---|---|
  | **Claude 1** | docs / repo hygiene (`README.md`, `CODEX.md`, `.claude/`, `docs/`) |
  | **Claude 2** | demo UI / product surface (`frontend/`, demo routes) |
  | **Codex** | tests / evals (`backend/tests/`, `tests/`) |

  Anything outside your column: report it, don't edit it. Cross-cutting changes go
  through the main session (the coordinator) or an explicit owner handoff.

## Golden rules (all agents obey)

- **Contract-safe:** never break the live-meeting integration contract (CODEX.md):
  `ws/<conversation_id>` + SSE `/avatar/stream/<id>` + poll `/avatar/messages/<id>`,
  the `{type:"speak", text}` message handling, and the `recall_client` /
  `anam_client` signatures.
- **Demo key-free:** the offline demo must always run in `stub` + `hash` with no keys.
- **No secrets in git** (`.env*` gitignored; only `.env.example` tracked).
- **Meters off:** end sessions (Recall per-minute), and never leave the photoreal
  GPU box running (`gpu/stop.sh` / the auto-stop layers).
- **Synthetic data only** in `avatars/*/knowledge`.
- **Transcripts are PII** — memory + artifact store only, never logged. **Latency
  is the product** on the live path.
- Agents don't commit/push unless explicitly asked; they prefer writing durable
  artifacts to `docs/` over chat-only answers.

## The team

| Agent | Trigger it for… | Model |
|---|---|---|
| **product-strategist** | scoping an idea/complaint into a PRD, roadmap, prioritization | opus |
| **code-reviewer** | reviewing a diff/PR for contract/latency/PII/meter safety before merge | sonnet |
| **backend-infra** | deploy issues, latency regressions, prod errors, cost/infra tuning | sonnet |
| **growth-gtm** | positioning, ICP, messaging, landing/demo copy, outreach angles | sonnet |
| **market-intel** | competitor teardowns, market map, defensibility, "who else does this?" | opus |
| **fundraise-narrative** | pitch narrative, deck outline, TAM/SAM/SOM, investor updates | opus |
| **finance-unit-economics** | pricing, gross margin, break-even, per-minute cost model | sonnet |

Plus the existing engineering helpers: **avatar-author** (new avatar = new folder),
**backend-tester** (test + offline pipeline), **demo-runner** (smoke-test the demo).
The verification agents (backend-tester, demo-runner) are Bash+Read only — they run
and report; fixes go through the owning session.

## Where each writes (no ownership conflicts)

- product-strategist → `docs/product/`
- growth-gtm, market-intel → `docs/gtm/` (market-intel also `docs/research/`)
- fundraise-narrative, finance-unit-economics → `docs/fundraise/`
- code-reviewer, backend-infra → no docs of their own (review/run against the repo)

## Example prompts

- **Product:** "product-strategist: spec the smallest version of 'Laura flags a
  missing approval live in the meeting', grounded in decision.py/brain.py."
- **Review:** "code-reviewer: review the current branch — does anything touch the
  ws speak-contract, add latency to the live path, or log transcripts?"
- **GTM / Fundraise:** "growth-gtm: write the landing hero + 3 value props for the
  wedge vs. notetakers." · "fundraise-narrative: draft the 10-slide deck outline and
  a TAM/SAM/SOM with assumptions."
