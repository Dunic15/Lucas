# CLAUDE.md: working in the Laura repo

Laura is a **Callable AI Process Avatar** (joins Zoom/Meet/Teams, answers grounded
from process docs, drafts post-meeting artifacts). Full context:
[`.claude/CONTEXT.md`](.claude/CONTEXT.md). Architecture + seams: `README.md`.
Parallel-work + integration contract: `CODEX.md`.

**Hard constraints (never violate):** don't break the live-meeting contract
(`ws/<conversation_id>`, `{type:"speak",text}`, `recall_client`/`anam_client`
signatures); demo runs key-free (`stub`+`hash`); no secrets in git; end sessions to
stop the per-minute meter; synthetic data only in `avatars/*/knowledge`; transcripts
are PII (memory only, never logged); latency is the product on the live path.

---

## When to use the agent team (auto-routing)

The specialized agents live in [`.claude/agents/`](.claude/agents/). **Route to an
agent when a task clearly matches its domain AND is a substantial, self-contained
piece of work that produces a durable artifact.** Otherwise do it inline.

**Delegate to an agent when the task is…**

| The task is about… | Use agent |
|---|---|
| Scoping an idea/complaint into a spec, or a roadmap/prioritization call | **product-strategist** |
| Reviewing a diff/branch/PR before merge (contract, latency, PII, meter safety) | **code-reviewer** |
| A deploy issue, latency regression, prod error, or cost/infra tuning | **backend-infra** |
| Positioning, ICP, messaging, landing/demo copy, outreach | **growth-gtm** |
| Competitor teardown, market map, "who else does this", defensibility | **market-intel** |
| Pitch narrative, deck outline, TAM/SAM/SOM, investor updates | **fundraise-narrative** |
| Pricing, gross margin, break-even, per-minute cost model | **finance-unit-economics** |
| Scaffolding a **new avatar** (folder + synthetic SOPs) | **avatar-author** |
| Running the test suite / offline pipeline checks | **backend-tester** |
| Smoke-testing the demo end-to-end | **demo-runner** |

**Do it INLINE (no agent) when…**
- It's a **quick edit or one-file change** you can finish in a few tool calls.
- You're **mid-task and already hold the context**: spawning restarts cold and
  re-derives what you already know (the expensive path).
- It's **debugging / a direct question / a small fix**: faster inline.
- The task **spans several agents' domains at once**: handle it yourself and only
  pull in an agent for a genuinely separable chunk.

**Rule of thumb:** a *substantial, single-domain deliverable* (a GTM doc, a market
analysis, a fundraise section, a pre-merge review, a new avatar) → agent. A *tweak,
a debug, or something you're already in the middle of* → inline. When unsure, do it
inline and mention the relevant agent as an option.

Every agent reads `.claude/CONTEXT.md` first and obeys the golden rules in
[`.claude/agents/README.md`](.claude/agents/README.md). Agents don't commit/push
unless asked; they prefer writing artifacts to `docs/`.

---

## Claude Code guardrails (mechanical, not prose)

- **AWS access is deliberate and pre-approved.** `.mcp.json` keeps
  `READ_OPERATIONS_ONLY=false` because Claude drives App Runner deploys/config
  here, and `mcp__aws-api__call_aws` sits in `permissions.allow`: the owner
  asked (2026-07-08) to never be prompted for AWS calls. The safety bar moves
  to behavior: destructive/irreversible AWS ops (deletes, teardown of running
  services) still deserve a heads-up in chat before running.
- **Parallel sessions: check before you push/deploy.** No live channel links
  concurrent Claude/Codex sessions; they share this repo's auto-memory but only
  async (loaded at session start, not live). Before `git push` to `main` or a
  prod `update-service`, confirm another session isn't mid-flight: `aws apprunner
  list-operations` on `laura-backend` (a deploy running?) + `gh pr list`. App
  Runner serializes deploys and errors on a concurrent op; wait for `RUNNING`,
  don't fight it. And never restart prod without confirming no live meeting
  (`GET /health` → `active_sessions:0`, cross-check Recall for in-call bots).
- **PreToolUse hooks** (`.claude/hooks/guard.py`) mechanically enforce the two
  rules that cost money or leak PII: commits are blocked if the staged diff
  contains an API-key-shaped string; edits/commits are blocked if they add
  `print`/`logger` calls on transcript content. Docs (`.md`/`.txt`) and files
  outside the repo are exempt. The guard fails open (a crash never blocks work).
- **Permissions** deny `git push --force` and `rm -rf` outright; safe repetitive
  commands (`pytest`, `git diff/log/status`, `uvicorn`, localhost `curl`) are
  pre-allowed; `.env` reads prompt first.
- **Slash commands** for the recurring asks: `/code-review` (working-diff review
  against the contract/latency/PII/meter checklist), `/smoke-demo`,
  `/test-backend`, `/check-sessions` (orphaned billing sessions).
- **Verification agents can't edit**: `backend-tester` and `demo-runner` are
  Bash+Read only; they run and report; fixes go through the main session.
