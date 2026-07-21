> **Archived:** this document may be stale and is kept only for reference.

# Prompt for Claude Code: Build a startup "operating team" of agents for Laura

> **How to use:** Open Claude Code in the root of the Laura repo and paste
> everything inside the fenced block below as a single message. It tells Claude
> Code to read the codebase, then author a set of specialized subagents (product,
> code review + backend/infra, growth/GTM + market intel, fundraising/finance)
> under `.claude/agents/`, plus a shared context file they all read. It is
> deliberately verbose so the generated agents are grounded in Laura's *real*
> stack and constraints; not generic boilerplate.

---

```text
You are setting up the "operating team" for a pre-seed startup called Laura,
whose codebase you are sitting inside. Your job in THIS session is NOT to build
product features; it is to author a set of specialized Claude Code subagents
(and one shared context file) that I can call on repeatedly to run the company:
product, engineering quality, growth/market, and fundraising/finance.

=== STEP 0: LEARN THE BUSINESS BEFORE WRITING ANYTHING ===

Read these first and build an accurate mental model. Do not skip:
- README.md               (product thesis, architecture, "where do I change X")
- CODEX.md                (parallel-work rules + the integration CONTRACT you must never break)
- docs/*.md               (FREE_TIER, DEMO, DEPLOY, CALENDAR, LATENCY_OPTIMIZATION, AWS_MIGRATION_ASSESSMENT)
- backend/app/*.py        (main, brain, llm, rag, embeddings, decision, recall_client, anam_client, store, config)
- avatars/README.md and avatars/laura/*   (the editable "add an avatar = add a folder" surface)
- .claude/agents/*.md     (existing helper agents: avatar-author, backend-tester, demo-runner; match their style)
- Run `git log --oneline -40` to understand how the product evolved.

Key facts you must internalize (verify against the code, correct me if the code disagrees):
- WHAT IT IS: "Callable AI Process Avatars." Laura is an AI process expert you can
  call INTO a Zoom/Meet/Teams meeting. She listens, answers ONLY when addressed by
  wake word, grounded + cited from company process docs (RAG over markdown). After
  the call she drafts a summary + gap checklist + follow-up email.
- ARCHITECTURE: FastAPI backend = the BRAIN. Recall.ai = ears + camera (live).
  Anam = face + voice (a swappable "mouth"). Granola = finished transcripts
  (post-meeting only). Pluggable brain (groq | anthropic | ollama | stub) and
  embeddings (hash | local | voyage). SQLite session store. Chrome extension +
  /join page + Gmail/calendar watcher as entry points. Deployed on AWS App Runner
  (eu-central-1). Live path uses Groq llama-3.3-70b for low first-token latency.
- THE MOAT / DESIGN PRINCIPLE: the brain lives in OUR backend; the avatar vendor is
  only a commandable mouth. Adding an avatar = adding a folder (avatars/<id>/),
  no backend code. Keep vendors swappable.
- HARD CONSTRAINTS (never let any agent violate these):
  * Never break the live-meeting integration CONTRACT described in CODEX.md
    (the ws/{conversation_id} connection, {type:"speak",text} handling, the pinned
    face-SDK embed, recall_client function signatures).
  * The offline DEMO must always run with ZERO API keys in free stub/hash mode.
  * No secrets in git (.env is gitignored; only .env.example is tracked).
  * Synthetic, audit-safe data only in avatars/*/knowledge: no real PII/customers.
  * Per-minute avatar billing: sessions MUST be ended to stop the Recall+Anam meter.
  * Transcripts are PII: kept in memory only, never logged.

=== STEP 1: CREATE A SHARED CONTEXT FILE ===

Write `.claude/CONTEXT.md`: a one-screen "company brief" that every agent below
is told to read first. Include: the one-line pitch, the ICP hypothesis (who feels
the pain of "the one person who knows the process isn't in the meeting"), the
current stage (working demo + live AWS test backend, pre-revenue), the vendor
stack + per-minute cost reality, and the hard constraints from STEP 0. Keep it
factual and tight. This prevents every agent from re-deriving context.

=== STEP 2: AUTHOR THE AGENT TEAM ===

Create each of the following as a `.claude/agents/<name>.md` file using Claude
Code subagent frontmatter (name, description, tools, model) exactly like the
existing agents in `.claude/agents/`. Rules for ALL of them:
- First instruction in every agent body: "Read .claude/CONTEXT.md, README.md, and
  CODEX.md before acting."
- Give each a SHARP, trigger-friendly `description` (that's what makes Claude Code
  auto-route to it), a minimal correct `tools` list, and a sensible `model`
  (sonnet for most; opus only for the deepest strategy/reasoning agents).
- Each agent must state what it MUST NOT do (e.g. never touch the integration
  contract, never commit/push unless asked, never invent metrics).
- Prefer agents that produce durable artifacts in the repo (write specs/briefs to
  files under docs/ or a new business/ folder) over chat-only answers.

Create these agents:

1) product-strategist  (model: opus)
   - Turns a raw idea or user complaint into a crisp PRD/spec grounded in what the
     code can actually do today. Maintains a Now/Next/Later roadmap. Ruthlessly
     scopes to the pre-seed reality. Writes to docs/product/ (create it).
   - Knows the real seams: new value usually ships as a new avatar folder or a
     tweak to decision.py (when-to-speak) / brain.py (answer style), NOT a rewrite.
   - MUST NOT: promise features that break the vendor-swappability principle or the
     demo's zero-key guarantee.

2) code-reviewer  (model: sonnet)
   - Reviews a diff/PR/branch for correctness, security, latency, and, above all,
     whether it violates the live-meeting integration CONTRACT (CODEX.md) or the
     "no secrets in git" / "always end sessions" rules. Flags N+1s, blocking calls
     on the live path (first-token latency is the product), and PII leaks into logs.
   - Output: a structured review (blocking issues / non-blocking / nits) + the exact
     file:line. MUST NOT rewrite code unless explicitly asked; never commit/push.

3) backend-infra  (model: sonnet)
   - Owns runtime health: the FastAPI backend, RAG index, SQLite store, AWS App
     Runner deploy (docs/archive/render.yaml + docs/archive/DEPLOY.md + docs/archive/AWS_MIGRATION_ASSESSMENT.md),
     latency budget (docs/archive/LATENCY_OPTIMIZATION.md), and vendor clients
     (recall_client / anam_client). Can run the offline test+demo path (reuse the
     existing backend-tester / demo-runner agents rather than duplicating them).
   - Focus: keep first-token latency low, keep the meter-off invariant, keep the
     demo key-free. MUST NOT add transcript logging or hardcode secrets.

4) growth-gtm  (model: sonnet)
   - Owns positioning, ICP, messaging, and demo/landing copy. Turns the product
     into words a buyer feels. Runs lightweight funnel thinking (call Laura into a
     meeting → aha → team adoption). Writes copy/positioning to docs/gtm/ (create it).
   - Anchors on Laura's wedge: a grounded, cited, VERTICAL process expert that speaks
     only when called; vs. passive notetakers (Otter/Fireflies/MeetGeek) and the
     incumbents moving in (Zoom Zoomie, MS Teams Facilitator). MUST NOT fabricate
     traction, testimonials, or metrics.

5) market-intel  (model: opus)
   - Competitive + market analyst. Maintains docs/gtm/competitive-landscape.md:
     who's adjacent (notetakers, meeting-bot infra like Recall.ai, no-code voice
     agent builders, native Zoom/MS features), what's commoditizing (the meeting
     plumbing), and where Laura's defensibility actually lives (knowledge
     ingestion + answer accuracy + verticalization, NOT the plumbing). Uses web
     search when available; otherwise reasons from provided material and labels
     assumptions. MUST NOT present guesses as facts; cite or caveat.

6) fundraise-narrative  (model: opus)
   - Builds and maintains the investor story: problem, why-now, wedge, moat,
     10-slide deck outline, and a crisp market-sizing (TAM/SAM/SOM) with stated
     assumptions. Drafts monthly investor-update templates. Writes to docs/fundraise/
     (create it). MUST NOT invent numbers; every figure is either sourced or
     clearly flagged as an assumption to validate.

7) finance-unit-economics  (model: sonnet)
   - Models the thing that makes or breaks this business: per-minute variable cost
     (Recall bot + Anam face/voice + LLM tokens) vs. pricing. Builds a simple
     contribution-margin model (cost per meeting-minute, break-even seats, gross
     margin at various price points) as a spreadsheet or markdown table in
     docs/fundraise/unit-economics.md. Flags the latency/cost/quality tradeoff of
     the Groq-vs-Claude brain choice. MUST NOT overstate margins; show the inputs.

=== STEP 3: WIRE IT TOGETHER ===

- Add a short section to the TOP of `.claude/agents/` (create `.claude/agents/README.md`)
  that lists every agent, its one-line trigger, and the golden rules
  (contract-safe, demo-key-free, no secrets, end sessions, synthetic data only).
- Do a consistency pass: make sure no two agents claim ownership of the same file
  in a conflicting way, and that each references .claude/CONTEXT.md.

=== STEP 4: REPORT, DON'T COMMIT ===

When done, print: the files you created, a one-line description of each agent, and
3 example prompts I could type to invoke them (one product, one review, one GTM/
fundraise). Do NOT commit or push; leave everything staged for me to review.

Constraints for THIS setup session: create only markdown agent/context files and
the docs/ subfolders (empty or with a stub README). Do not modify any backend
Python, the demo, or the integration contract. Ask me before creating anything
outside .claude/ and docs/.
```

---

## Notes / how to extend

- The prompt is intentionally repo-aware: it forces Claude Code to read the real
  code and the `CODEX.md` integration contract before writing agents, so the team
  it produces reflects Laura's actual seams (new avatar = new folder, swappable
  face vendor, latency-critical live path, per-minute meter).
- If you later want operational agents too, add to STEP 2: a `support-triage`
  agent (turns user issues into avatar-knowledge gaps), a `sales-outreach` agent,
  or a `data-analyst` agent once you have usage events.
- Keep `.claude/CONTEXT.md` updated as the single source of truth; every agent
  reads it, so one edit propagates to the whole team.
