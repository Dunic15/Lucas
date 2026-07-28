# Repository structure — where everything lives

The map of the Laura repo, accurate as of the **#329 backend refactor** ("domain
packages + main.py decomposition, moves-only, contract untouched"). This is the
"where is X?" doc; for how the engine *works*, see
[ARCHITECTURE_CURRENT.md](ARCHITECTURE_CURRENT.md).

> **What #329 changed:** the previously flat `backend/app/*.py` namespace is now
> **14 domain packages**. About 50 old top-level modules became **4-line
> compatibility shims** (`sys.modules[__name__] = <package module>`) so every old
> import path (`import app.store`, `app.config`, `app.brain`, …) still resolves.
> Nothing broke; the flat citations in older docs are just structurally stale.

---

## `backend/` — the brain (deployed to App Runner)

| Path | What it owns |
|---|---|
| `app/main.py` | App assembly (includes ~20 routers) **+ the live-meeting contract handlers**, deliberately kept here (see below). ~4,400 lines. |
| `app/api/` | HTTP routers carved out of `main.py`: `dashboard.py` (biggest), `sessions.py`, `oauth.py` (`/oauth/{google,asana,jira}`), `org_api.py`, `org_avatars_api.py`, `pipedream_api.py`, `console.py` (`/`, `/demo/*`, `/live/*`), `pages.py`, `health.py`, `meetings.py`, `granola.py`, `avatars_api.py`, `deps.py` (shared helpers). |
| `app/brain/` | Reasoning: `engine.py` (`answer_question` live grounded/cited answers + `post_meeting` summary), `llm.py` (pluggable provider), `rag.py`, `embeddings.py`, `tools.py`, `tool_registry.py`. |
| `app/meeting/` | Live-meeting logic: `lifecycle.py` (session lifecycle), `decision.py` (when-to-speak), `meeting_state.py` (regex process tracker), `end_of_turn.py`, `emotion.py`. |
| `app/actions/` | Action Control Plane: `executor.py`, `action_plane.py`, `ledger.py`, `outbox.py` + `outbox_pg.py` (durable), `action_reconcile.py`, `approval_runtime_guard.py`, `scheduler.py`, `autopilot.py`. |
| `app/knowledge/` | Company Brain: `dal.py`, `ingest.py`, `storage.py`, `router.py` (`/org/knowledge` + dashboard twin). |
| `app/browser/` | Browser Operator (B0): `operator.py` (canonical state machine), `browserbase_provider.py`, `fake_provider.py`, `policy.py`, `coordinator.py`, `planner.py`, `recipes.py`, `router.py`. |
| `app/cedric/` | Cedric×Laura Slack-orchestrator glue (own `README.md`): `callback.py`, `integration.py`, `mcp.py`, `secret_registry.py`, `chat_responder.py`, `install_state.py`. Upstream seams in `main.py` are single lines tagged `# CEDRIC`. |
| `app/openclaw/` | OpenClaw full-executor experiment: tenant gate, run ledger, dashboard/API router, and tool bridge. Flag-gated; active orgs stamp `execution_route=="openclaw"` and suppress legacy executors. |
| `app/integrations/` | Vendor clients: `recall_client.py`, `google_client.py`, `asana_client.py`, `jira_client.py`, `gemini_ears.py`, `graphiti_client.py`, `anam_client.py`, `tts.py`, `gmail_watcher.py`, `granola_client.py`, `drive_client.py`. |
| `app/persistence/` | Data layer: `store.py` (SQLite dev store), `control_plane.py` (Postgres control plane, RLS), `billing.py` (Stripe), `org_avatars_pg.py`. |
| `app/core/` | Cross-cutting: `config.py` (all env/flags), `auth.py`, `entitlements.py`, `security.py`, `crypto.py`. |
| `app/avatar/` | `avatars.py`, `avatar_resolver.py`, `avatar_overlay.py` (identity resolution + per-org overlay). |
| `app/runtime/` | `native_runtime.py`, `gpu_runtime.py`, `runpod_runtime.py` (execution + GPU box lifecycle). |
| `app/datafoundation/` | Data Foundation (DF0-DF1): `dal.py`, `connectors.py`, `resolver.py`, `sync.py`, `router.py` (`/org/data`). |
| `app/demo_mvp/` | Northstar demo MVP: `northstar_provider.py`, `execute.py`, `ingest.py`, `narration.py`, `router.py` (`/org/demo`). |
| `app/pipedream_client.py`, `app/pipedream_executor.py` | **Real top-level modules** (not shims): Pipedream Connect — managed-auth connections + the Connect-Proxy execution path (flag-gated, `execution_route=="pipedream"`). |
| `app/browser_meeting.py` | **Real top-level module:** browser-based meeting join path. |
| `alembic/` | Postgres control-plane migrations, `0001_org_id_spine` → `0021_openclaw_experiment`. `backend/alembic.ini`. |
| `scripts/` | Dev/ops CLIs: `ask.py`, `simulate.py`, `ingest.py`, `recall_check.py`, `browser_b1_smoke.py`. |
| `tests/` | ~140 `test_*.py` files, ~1,575 tests. Runs **key-free** in CI (`BRAIN_PROVIDER=stub`, `EMBEDDING_PROVIDER=hash`, embedded Postgres for RLS). |
| `spikes/` | Throwaway prototypes (`vertex_live*`), **not wired into the app**. |

### The live-meeting contract handlers kept in `main.py`

WS `/ws/{conversation_id}` · GET `/avatar/stream/{conversation_id}` · GET
`/avatar/messages/{conversation_id}` · POST `/avatar/speaking/{conversation_id}` ·
POST `/webhooks/recall` · POST `/webhooks/recall-calendar` · WS
`/realtime/recall-audio[/{cap_path}]`. Changing these can break running meetings —
see [CODEX.md](../CODEX.md).

---

## Everything else (top level)

| Path | What it is | Status |
|---|---|---|
| `frontend/` | Face + product pages: `talk/photoreal/avatar/live.html` (renderers), `dashboard.html` (Control Center), `login/join/meetings/demo.html`, `privacy/terms.html`, vendored `<id>.glb` head models, `fixtures/` (browser-operator UI states). | live |
| `avatars/` | One folder per avatar — `laura`, `cedric`, `petra`, `sff`, `duccio` (internal). `avatar.yaml` + `knowledge/` + `process_templates/` + `about/`. See [avatars/README.md](../avatars/README.md). | live |
| `gpu/` | Photoreal track: `server.py` (one `AVATAR_ENGINE` seam: `stub`/`musetalk`/`ditto`), Ditto adapter + `Dockerfile.ditto` (production), cost-control scripts, `gpu/assets/reference-*.jpg`. | live (Ditto) |
| `relay/laura-ears/` | Cloudflare Worker "ears" — relays Recall audio → Gemini Live → `/webhooks/recall` (App Runner refuses inbound WS). | live |
| `demos/northstar/` | Isolated synthetic company for the end-to-end demo; imported by `app/demo_mvp` when the demo is enabled. | live |
| `extensions/laura-meet/` | Chrome MV3 "Send Laura" overlay for Meet/Zoom/Teams. | live |
| `lovable/` | Lovable-generated marketing landing site (separate toolchain; **not** the app in `frontend/`). Commits sync back to Lovable — don't rewrite history. | semi-active |
| `etc/litestream.yml` | Continuous S3 replication of the SQLite store. | live (infra) |
| `scripts/` | Repo-level ops: `start-with-litestream.sh` (prod boot wrapper), `serve.sh`, `validate_rls.py`, `latency_probe.py`. Distinct from `backend/scripts/`. | live |
| `docs/` | Architecture, product (PRDs/roadmap), infra, GTM, research, fundraise, finance + `docs/archive/` (intentional history). | live |
| Root | `Dockerfile` (ships `backend/`+`frontend/`+`avatars/` only), `requirements.txt`, `.env.example`, `CLAUDE.md`, `CODEX.md`, `.claude/CONTEXT.md`, `.mcp.json`. | live |

---

## Known-stale / tidy candidates

Tracked here so it's honest; fixing these is low-risk cleanup, not behavior change.

- **Docs citing pre-#329 flat paths** (`brain.py`, `meeting_state.py`, `config.py`,
  `decision.py`) — being migrated to the package paths above. Affected:
  `ARCHITECTURE_CURRENT.md` "Key files" table, `.claude/CONTEXT.md`, `CODEX.md`.
- **`ARCHITECTURE_CURRENT.md`** — its "Key files" table predates the package split.
- **`.claude/CONTEXT.md`** — the brain-provider line still calls Groq/llama the live
  default; prod actually runs Cerebras `gemma-4-31b` (fixed in the README).
- **MuseTalk → Ditto** — older prose names MuseTalk; production photoreal is Ditto.
- **`REPO_HYGIENE.md`** — the previous "current vs stale" inventory (2026-07-14) is
  itself well behind HEAD; **this file supersedes it** as the structure map.
- **`voice-previews/*.mp3`** — tracked despite the `.gitignore` rule; the chosen
  voice already lives in `ids.json` + `avatars/*/avatar.yaml`. Safe to remove.
- **`gpu/assets/reference.jpg`** — byte-identical duplicate of `reference-laura.jpg`
  (legacy default placeholder).
- **`frontend/laura-avatar.jpg`** — appears unused (portrait serving goes through
  `gpu/assets` via `/laura-reference.jpg?avatar_id=`).
- **`docs/assets/lookdev/`** — 22 avatar screenshots; thin to a couple.
- **`app/<legacy>.py` shims** — intentional back-compat; can be pruned once all
  imports move to package paths.
