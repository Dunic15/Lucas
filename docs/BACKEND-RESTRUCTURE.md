# Backend restructure — status & resume-plan

*Moves-only packaging of the flat `backend/app/` into cohesive domain packages,
plus the start of the `main.py` decomposition. Every step is behavior-preserving
and verified against a green baseline (**1310 passed / 1 xfailed**).*

> **Where it lives:** branch `restructure/repo-structure` (7 commits, `d0fc228 → ef276e6`),
> based on `clarify-before-create @ 2933447`. This is the **archived SFF-Studio
> fork** — the work is local structure only; it does **not** deploy (prod is
> upstream `Dunic15/Laura`) and cannot be pushed to origin (archived).

---

## Done

### Domain packages (moves-only, compat shims at every old path)
| Commit | Package | Modules |
|---|---|---|
| `d0fc228` | `app/integrations/` | recall_client, google_client, asana_client, granola_client, drive_client, anam_client, gemini_ears |
| `a5eafaf` | `app/actions/` | ledger, outbox, outbox_pg, executor, action_plane, action_deps, action_reconcile, autopilot, scheduler, workflow* |
| `2d67255` | `app/meeting/` | meeting_state, decision, emotion, end_of_turn |
| `bad42fd` | `app/core/` | config, auth, crypto, security, entitlements |
| `f6d697d` | `app/brain/` | engine*, llm, rag, embeddings, tools, tool_registry |
| `8ed808f` | `app/persistence/` | store, billing |

`*` `workflow` = the old `actions.py`; `engine` = the old `brain.py` (both renamed
to avoid a module-vs-package name clash — see Gotchas).

### main.py decomposition — COMPLETE (main.py 6346 → 3978, −37%)
Executed from an ultracode-generated, grep-verified playbook (`api/*` routers).
Every step verified: **1310 passed / 1 xfailed**.
| Commit | Slice | main.py |
|---|---|---|
| `ef276e6` | `api/pages.py` — static/file-serving routes | 6346 → 6226 |
| `8643328` | `api/granola.py` — /granola/* | 6226 → 6213 |
| `14101f6` | `api/oauth.py` — /oauth/* + `api/deps.py` (first hoist-then-extract) | 6213 → 5780 |
| `ffb197f` | `api/avatars_api.py` — /avatars* | 5779 → 5730 |
| `de197fe` | `control_plane`→persistence/, `dashboard`→api/ (git-mv+shim) | — |
| `396b1a0` | `api/meetings.py` — /ledger, /meetings*, /avatar | 5729 → 5658 |
| `6475c64` | **`meeting/lifecycle.py`** — hoist the finalize/dispatch core (24 syms) | 5658 → 4699 |
| `0af2663` | `api/health.py` — /health, /recall/status, /gmail/status … | 4699 → 4597 |
| `0a5c68c` | `api/console.py` — /, /demo/*, /live/* | 4597 → 4435 |
| `f11d8bc` | `api/sessions.py` — /sessions/* (Path A via `import app.main as _main`) | 4434 → 3978 |

`api/deps.py` holds the cross-cutting helpers (`EMAIL_RE`, `_split_emails`,
`_calendar_target_emails`, `_line_for`, `_gmail_state`). `meeting/lifecycle.py`
holds the shared meeting-lifecycle core; `main.py` re-imports the names its
stayers call (identity preserved for monkeypatch).

**The live-meeting CONTRACT stays in `main.py` — by design.** The ultracode
analysis' verdict (LEAVE it): the ws/webhook/realtime handlers are inseparable
from the lifecycle+lines core, would break ~30 test files, and touch CLAUDE.md's
hard rule for no deploy benefit on an archived fork. `main.py` is now app
assembly + `include_router`s + the live-meeting contract.

Final `backend/app/` layout: `api/ core/ actions/ meeting/ brain/ integrations/
persistence/ runtime/` + pre-existing `knowledge/ cedric/`; still-flat: `main.py`
(assembly + contract), `org_api.py` (already a router), `avatars.py`.

---

## The repeatable method (recipe)

**Never move without a green baseline. Run the suite before and after every step:**
```bash
GRAPHIFY_SKIP_HOOK=1 BRAIN_PROVIDER=stub EMBEDDING_PROVIDER=hash \
  .venv/bin/python -m pytest backend/tests -q -p no:cacheprovider   # expect: 1310 passed, 1 xfailed
```
(`GRAPHIFY_SKIP_HOOK=1` avoids the post-commit graph rebuild churn.)

**Packaging a domain** (`scripts` live in the session scratchpad; logic is simple):
1. Check `__file__` usage in the modules first (`grep -n __file__`). Any
   `Path(__file__)...parents[N]` needs `N+1` after going one level deeper.
2. `mkdir app/<pkg>`, write `__init__.py` (docstring).
3. `git mv app/<m>.py app/<pkg>/<m>.py` for each module.
4. Rewrite the moved files' imports: **siblings → `.x`, everything else → `..x`**
   (the `..x` resolves through the shim). Split mixed `from . import a, b, c` lines.
5. Write a shim at each old path:
   ```python
   import sys as _sys
   from app.<pkg> import <m> as _mod
   _sys.modules[__name__] = _mod   # app.<m> IS app.<pkg>.<m> — identity preserved
   ```
6. Smoke-import (`import app.main` + shim identity), then full suite. Commit.

**Splitting a route group out of `main.py`:**
1. Pick a cohesive, contiguous group (avoid the contract handlers).
2. Copy the block into `app/api/<group>.py`; `router = APIRouter()`; `@app.` → `@router.`.
   Derive shared consts (`FRONTEND_DIR`, etc.) from `app.core.config`, **never**
   import them from `main` (circular).
3. In `main.py`: delete the block, add `from .api import <group>` +
   `app.include_router(<group>.router)`.
4. **Verify with TestClient (real requests), not `app.routes`** — this repo runs a
   custom FastAPI where `include_router` defers registration (`_IncludedRouter`),
   so routes don't appear on `app.routes` until request time.
5. Fix any test that calls a moved handler **directly** (e.g.
   `main.photoreal_reference` → `pages.photoreal_reference`). Full suite. Commit.

---

## Gotchas (each cost a debugging cycle — watch for them)

- **`__file__`-relative paths break on a move.** `core/config.REPO_ROOT`
  (`parents[2]→[3]`) and `persistence/store.STORE_PATH` (`parents[1]→[2]`) both
  needed a depth bump. Import-clean but wrong at *runtime* (avatars/DB paths).
- **Module-vs-package name clash.** A module `x.py` can't coexist with a package
  `x/`. `actions.py`→`workflow.py`, `brain.py`→`engine.py`; a re-export in
  `__init__` can't preserve monkeypatch identity, so ~20 `brain` test consumers
  were retargeted to `app.brain.engine`.
- **Custom FastAPI `include_router`** defers to `_IncludedRouter` — verify routes
  by request, not introspection.
- **Tests that call route handlers directly** must be retargeted to the new module.
- **Shared working tree / HEAD.** A branch reset came from outside this session
  once (parallel process). Commit promptly; re-check `git branch --show-current`
  before each commit.

---

## Remaining work (ordered resume-plan)

### A. Finish `main.py` (6226 lines → target < 500 of app assembly)
Extract these route groups into `app/api/*.py`, **easiest first, contract LAST**:
1. `demo.py` — `/`, `/demo/ask`, `/demo/post_meeting`, `/demo/sample`
2. `health.py` — `/health`, `/health/vendors`, `/recall/status`, `/gmail/status`, `/gemini-ears/status`, `/internal/ears-config/{capability}`
3. `live.py` — `/live/ask`, `/live/act`, `/live/error`, `/live/token`
4. `oauth.py` — `/oauth/google/*`, `/oauth/asana/*`
5. `granola.py` — `/granola/notes`, `/granola/transcript`
6. `avatars_api.py` — `/avatars`, `/avatars/{id}/brain-mode`, `/avatar`, `/meetings*`, `/ledger`
7. `sessions.py` — `/sessions/start`, `/sessions/{bot_id}/{context,end,cancel,deliver,artifact,redeliver}` (more shared state — go carefully)
8. **CONTRACT (do last, maximum care, keep signatures/paths byte-identical):**
   `ws/{conversation_id}`, `/avatar/{stream,messages,speaking}/{conversation_id}`,
   `/webhooks/recall`, `/webhooks/recall-calendar`, `/realtime/recall-audio*`.
   *CLAUDE.md hard rule: do not break the live-meeting contract.* Consider leaving
   these in `main.py` if the risk/benefit doesn't justify moving them.

Watch for shared module-level state/helpers used across groups (request models,
brain helpers, the session registry) — hoist them to a small shared module
(e.g. `app/api/deps.py` or `app/core/`) before extracting the routes that use them.

### B. Split the other monoliths (same route/section technique)
- `dashboard.py` (2402 lines — much of it HTML; move markup to `frontend/`/templates)
- `control_plane.py` (1809)

### C. Optional: fold the ~10 remaining flat singletons
`cedric_mcp → cedric/`, `tts / gmail_watcher / vendor_health → integrations/`,
`runpod_runtime / gpu_runtime → a runtime/ group`, `org_api → api/`, `avatars` (its own or `core/`).

### D. Cleanup (after everything is on new paths)
Delete the compatibility shims in one pass and update call-sites to import from the
new package paths directly (grep for `from app.<old> import` / `import app.<old>`).
