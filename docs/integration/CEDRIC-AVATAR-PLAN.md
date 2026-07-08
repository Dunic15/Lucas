# Cedric → first product avatar on the Laura platform — integration plan

**Status: PLAN — nothing implemented yet.** Written 2026-07-08 after auditing all
three repos. Companion contract doc: [`SURFACE-API.md`](SURFACE-API.md) (ours) vs
Ben's `Laura-API.md` (Slack, 2026-07-08) — reconciled in §4.

---

## 1. Where things actually stand (verified, not assumed)

| Repo | State |
|---|---|
| `Dunic15/Laura` (main @ `76af313`) | Platform. No inbound auth, no webhooks, `bot_name` hardcoded "Laura" (`recall_client.py:253`), global tool layer (`tools.py`: calculator/date_math/lookup_record), `knowledge_packs` already supported (`avatars.py`). Artifact **stores the raw transcript** (`main.py:909`). |
| `SFF-Studio/Laura-Cedric` (`cedric-fork` @ `b257f88`) | **Ben is far ahead of "trying".** Fork = *today's* Laura main (`76af313` merged in this morning) + 4 clean commits: `avatars/cedric/` pack, `backend/app/cedric/` package (auth, callbacks, cancel, context-in), 12 offline tests, root Dockerfile. All shared-file edits are one-line `# CEDRIC` hooks. Fold-back is essentially a plain git merge. |
| `SFF-Studio/Cedric` (main @ `853097c`) | Slack AI-employee (Next.js/Vercel, Claude Opus, Neon+pgvector, Pipedream, 41 tools). **Zero Laura code**: no `.claude/skills/cedric-laura/` (referenced in Ben's Slack message but absent from the pushed repo), no connector, no webhook receiver. Cedric#4 is still 100% to build. |

**What Ben already built in the fork (fold-back inventory):**

```
avatars/cedric/                       avatar.yaml (wake word "cedric", George voice
                                      JBFqnCBsd6RMkjVDRZzb, knowledge_packs: [sff]) + knowledge/
backend/app/cedric/                   integration.py (Bearer auth via LAURA_API_TOKEN, 32KB brief cap,
                                      409 per meeting_url, context/context_url/callback_url/external_ref,
                                      autopilot skip for orchestrated sessions)
                                      callback.py (session.status best-effort; session.ended retried
                                      5/25/120s; HMAC X-Laura-Signature t=<ts>,v1=…; one-redirect-hop fix)
backend/tests/test_cedric_integration.py   12 tests, fully offline
Dockerfile                            root, python:3.12-slim, for App Runner/ECR
--- one-line # CEDric hooks in: main.py (+ /cancel endpoint), config.py (5 LAURA_* vars),
    store.py (Session.integration + sqlite migration), recall_client.py (bot_name param
    + delete_bot), brain.py (post_meeting(context=)), .env.example
```

His persona already encodes the right tool philosophy: *"You never take actions
during the call … say you will queue it for approval in Slack right after."*

---

## 2. Target architecture (why this shape)

Cedric becomes the **first external-product avatar** on the platform (the folders
`laura/` and `sff/` are personas; Cedric is another *product* riding Laura's
meeting technology). His 41 tools **stay in his runtime** — they are
Slack/approval/Pipedream-coupled TypeScript, and his agent loop takes seconds per
turn (8 tool rounds, no streaming): putting them on Laura's live path would kill
the latency budget. The integration is a **bridge**:

```
Slack user ──► Cedric (summon, behind propose_action) ──► POST /sessions/start
                                                            avatar_id=cedric + brief
   Cedric compiles brief from HIS memory (recall/profile/calendar)  ── context-in ──►
   Laura runs the meeting: Cedric face/voice/persona, grounded answers,
   live tools = Laura-side only (fast, deterministic)
   ◄── actions-out ── session.ended webhook (distilled artifact + external_ref echo)
Cedric turns actions[] into HIS tools: propose_action cards, DMs, calendar, email
```

Each product stays independent: Laura with no orchestrator = today's behavior;
Cedric with no Laura = today's Slack agent.

---

## 3. Phases

### P0 — Coordinate with Ben (before touching anything)
Laura-Cedric is HIS active workspace. Send him this plan; agree he freezes
`cedric-fork` (or tells us what's still in flight) before the fold-back merge.
His last two commits were cleanup/refactor — a good sign the work has settled.

### P1 — Fold-back merge (issue #50) — *small, unblocks everything*
Histories are shared, so in the platform repo:
`git remote add cedric git@github.com:SFF-Studio/Laura-Cedric.git && git fetch
cedric cedric-fork && git checkout -b claude/cedric-foldback && git merge
cedric/cedric-fork` → PR to main. Preserves Ben's commits/authorship. Then:
run full suite + his 12 tests + `/smoke-demo`; confirm key-free demo unchanged.

### P2 — Contract closure (rides on P1, same branch or immediate follow-up)
1. **PII fix (must-do):** strip `transcript` (keep everything distilled) from the
   artifact **copy** sent in `session.ended` and returned by the orchestrator
   `GET …/artifact`. Internal storage keeps it (memory brief needs it). Ben's
   `Laura-API.md` lists `transcript` in the artifact shape — push back: hard rule
   is *raw transcripts never cross the API*. If Cedric truly needs it, that's a
   deliberate opt-in decision for later, not a default.
2. **Set the keys in prod now:** `LAURA_API_TOKEN`, `LAURA_WEBHOOK_SECRET`,
   `LAURA_WEBHOOK_TOKEN`, `LAURA_CONTEXT_TOKEN` in SSM/App Runner. Empty token =
   open is fine locally; the hosted service must never run keyless again
   (closes the unauthenticated `/sessions/start` money hole).
3. **`GET /v1/avatars`** (trivial: `avatars.list_ids()` + name/role/wake_words)
   so Cedric can offer "who should join?" without hardcoding.
4. **Decisions to settle with Ben** (defaults proposed):
   - `display_name` request field: *skip for v1* — avatar.name is the tile name
     (his implementation). If added later it must ALSO drive the echo guard.
   - `extra_prompt`: *skip for v1* — `context.brief_markdown` covers it; revisit
     only if a surface needs persona-flavoring distinct from context.
   - `artifact_version` field: add `"artifact_version": 1` now (cheap, saves pain).

### P3 — Org-memory API (issue #48) — *parallel-safe*
New module `backend/app/org_api.py` + `tests/test_org_api.py`: `GET /org/brief`
(ledger.carryover_brief), `GET /org/actions` (ledger.open_by_meeting),
`POST /org/actions/{id}/resolve` (ledger.resolve_item), same Bearer gate.
Integration = a 2-line router include in main.py at merge time. This is what
Cedric#4's connector consumes ("what's still open from meetings?" in Slack).

### P4 — Cedric render + voice — *parallel-safe, frontend/assets only*
Today Cedric would join with **Laura's face** (`anam_avatar_id: ""` → global
fallback; the bot renders `talk.html`, hardcoded default `/laura.glb`).
1. **Model:** create a male Ready Player Me avatar → `frontend/cedric.glb`.
   RPM guarantees the ARKit blendshapes/visemes TalkingHead needs for lip-sync;
   a generated GLB (e.g. image→3D tools) almost certainly won't rig — stick to
   RPM for v1, restyle later.
2. **Wiring (zero backend change):** talk.html already accepts `?avatar_url=`;
   add a convention — try `/models/{avatar_id}.glb`, fall back to `/laura.glb`.
3. **Voice:** generate 3 ElevenLabs male candidates with the existing
   voice-preview workflow (`voice-previews/` + ids.json pattern), Duccio picks,
   swap `elevenlabs_voice_id` in `avatars/cedric/avatar.yaml` (1 line). Stock
   voices only (custom voices hit the plan-tier 402 we saw with Clara). Ben's
   pick (George) is the placeholder to beat.
4. Quick live check on `/talk?avatar_id=cedric&avatar_url=/cedric.glb`.

### P5 — Cedric-side build (Ben's repo, Cedric#4) + ops
- **Ben:** webhook receiver route (`app/api/laura/events/`) verifying
  `X-Laura-Signature`, artifact→Slack posting via `external_ref` echo
  (he owns channel mapping), summon tool behind `propose_action` (it costs
  money), Laura entry in `lib/tools/connectors.ts`, and the
  `.claude/skills/cedric-laura/` doc his message referenced (not in the repo yet).
- **Ops:** repoint/retire the `laura-cedric` App Runner service after merge-back
  (issue #51), archive Laura-Cedric, then decide **storage durability** — sqlite
  on ephemeral disk wipes org memory every deploy; cheapest fix Litestream→S3
  sidecar in the (new) Dockerfile. Needs a decision, not code, this week.

---

## 4. Contract deltas (ours vs Ben's) — resolved

| Topic | SURFACE-API.md (ours) | Ben's Laura-API.md / code | v1 resolution |
|---|---|---|---|
| Auth header | `X-API-Key` | `Authorization: Bearer` (built) | **Bearer** |
| Correlation | `metadata` ≤4KB | `external_ref` (built) | **external_ref** |
| Webhook sig | `sha256=<hmac>` | `t=<ts>,v1=<hex>` + 300s window (built) | **Ben's** |
| Artifact event | separate `artifact.ready` | artifact inside `session.ended` (built) | **Ben's** |
| Context | `extra_prompt` ≤2k | `context.brief_markdown` ≤32KB + `context_url` (built) | **Ben's** |
| Transcript in artifact | never | listed in his doc AND in payload today | **strip on the wire (P2.1)** |
| Client registry | single env key | per-client keys (doc), static map v1 | **single key v1**, registry later |
| Avatars list | `GET /v1/avatars` | absent | **add (P2.3)** |
| Org memory | brief/actions/resolve | absent | **add (P3, #48)** |
| Cancel + 409 | absent | built | **keep** |

## 5. Parallel Claude Code sessions (work simultaneously, no conflicts)

Every session in its **own git worktree** (`git worktree add ../laura-<lane>
<branch>`) — never two sessions in the same checkout; use `python3 -m pytest`
(stale venv shebangs). File ownership is disjoint by construction:

| Session | Branch | Owns (only) | Deliverable | Merge order |
|---|---|---|---|---|
| **S1 platform** | `claude/cedric-foldback` | `backend/app/**`, `backend/tests/**`, `Dockerfile`, `.env.example` | P1 merge + P2 (PII strip, avatars endpoint, artifact_version) | **1st** |
| **S2 render** | `claude/cedric-render` | `frontend/**`, `voice-previews/**` | P4: cedric.glb, talk.html model convention, voice candidates | 2nd/anytime (no overlap) |
| **S3 org-API** | `claude/org-memory-api` | `backend/app/org_api.py`, `backend/tests/test_org_api.py` (both NEW files) | P3 endpoints + tests; 2-line main.py include applied at merge | after S1 |
| **S4 ops/spike** | `claude/storage-durability` | `docs/infra/**` (doc only, no code) | Litestream-vs-Postgres decision doc + cost | anytime |

The one shared file is `avatars/cedric/avatar.yaml` (arrives with S1's merge;
S2's voice swap is a 1-line edit after S1 lands — S2 does assets meanwhile).
Cedric-side work (P5) is a different repo entirely — zero conflict; Ben (or a
5th session in a Cedric clone) runs it. Before any merge: `/code-review`, and
the `repo-orchestrator` agent arbitrates if lanes drift.

## 6. Open questions for Ben (short list — most of the old ones he answered by building)

1. Freeze `cedric-fork` for the fold-back merge — anything still in flight?
2. Transcript in the artifact: OK to strip on the wire (PII rule)? What does
   Cedric actually need beyond summary/actions/decisions?
3. `display_name`/`extra_prompt` as request fields: park for v1 (avatar.name +
   brief_markdown cover it) — agreed?
4. The `cedric-laura` skill referenced in your message isn't in the pushed repo —
   local, or still to write?
5. Summon flow behind `propose_action` (it spends money) — confirm.
6. Volume estimate (sessions/day) → drives the storage-durability choice.

### P6 — Cedric full-power (Session 3, stacked on S1) — *decided 2026-07-08*

Architecture ruling by the owner: **Laura is the main agent/architecture; every
new capability is a PLATFORM feature that propagates to all avatars; Cedric is
the first child product avatar, full-power.** Concretely:

1. **`queue_action` live tool** (platform-level, in `tools.py` next to
   calculator/date_math — every avatar gets it): when someone asks the avatar to
   DO something ("send the recap", "book a follow-up"), the tool captures
   `{action, owner, due}` in-memory on the session — instant, zero I/O on the
   live path — and the avatar acknowledges ("queued for approval in Slack right
   after the call"). Captured items flow into the artifact's `actions[]` and the
   ledger at finalize (existing path), so plain-Laura sessions benefit too.
2. **`action.requested` webhook** (in `backend/app/cedric/callback.py`, same
   discipline as `session.status`: fire-and-forget, single attempt, HMAC-signed,
   `external_ref` echo) — fires only when the session is orchestrated, so
   Cedric's Slack approval card can be ready before the meeting ends.
3. **Cedric persona upgrade** (`avatars/cedric/avatar.yaml`): keep Ben's
   identity ("same Cedric the team talks to in Slack") but full Laura-grade
   breadth — general knowledge, opinions, web search for current facts,
   grounded in brief + knowledge packs — plus the action-capture behavior.
   Never claims an action was executed; execution always happens in Slack
   behind Cedric's approval.
4. Face/voice come from Session 2 (cedric.glb + chosen ElevenLabs voice).

Session 3 branches FROM `claude/cedric-foldback` (needs Ben's package +
avatars/cedric) and starts only once Session 1's PR is open.

## 8. Explicitly NOT in this plan

- Importing/porting Cedric's TypeScript tools into Laura's runtime (latency +
  coupling + secrets say no; the bridge gives the same user story).
- Per-client key registry, `team_key` grouping, manager debrief (#49) — after v1.
