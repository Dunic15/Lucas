# Laura — Agent Team

The "operating team" for the company. Every agent reads
[`.claude/CONTEXT.md`](../CONTEXT.md), `README.md`, and `CODEX.md` first. Claude Code
auto-routes by the `description` field; you can also name an agent explicitly.

## Golden rules (all agents obey)
- **Contract-safe:** never break the live-meeting integration contract (CODEX.md):
  `ws/<conversation_id>`, `{type:"speak", text}`, the `speak()` echo, the pinned
  face-SDK embed, and the `recall_client` / `anam_client` signatures.
- **Demo key-free:** the offline demo must always run in `stub` + `hash` with no keys.
- **No secrets in git** (`.env` gitignored; only `.env.example` tracked).
- **End sessions** — the Recall + Anam meter runs per minute; never leave one open.
- **Synthetic data only** in `avatars/*/knowledge`.
- **Transcripts are PII** — memory only, never logged. **Latency is the product** on
  the live path.
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

## Where each writes (no ownership conflicts)
- product-strategist → `docs/product/`
- growth-gtm, market-intel → `docs/gtm/`
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
