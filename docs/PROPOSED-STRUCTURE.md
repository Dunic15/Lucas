# Proposed backend structure — grounded in upstream `Dunic15/Laura` @ main (latest)

*Checked against upstream's current tree (92 commits ahead of the fork; ~70 in the
last 2 days). **Upstream has done no restructuring** — `backend/app/` is 60+ flat
modules and the monoliths are still growing (`main.py` **6,720**, `dashboard.py`
**2,670**, `store.py` 2,626, `brain.py` 1,988, `control_plane.py` 1,809). This
proposal targets that live tree, extends the 6-domain layout already proven on the
fork, absorbs upstream's newer modules (`jira_client`, `graphiti_client`,
`browser/`, `datafoundation/`, `demo_mvp/`, `avatar_resolver/overlay`,
`native_runtime`, `org_avatars_*`), and adds the `api/` split for `main.py`.*

## Target layout

```
backend/app/
  main.py                     # < 400 lines: app assembly only (lifespan, middleware, include_router)

  api/                        # HTTP layer — routers split out of main.py & dashboard.py
    deps.py                   # shared route helpers (email targeting, gates, …)
    pages.py       ✅ done    # static/file-serving pages + avatar assets
    granola.py     ✅ done    # /granola/*
    oauth.py       ✅ done    # /oauth/google/*, /oauth/asana/*
    avatars_api.py ✅ done    # /avatars, /avatars/{id}/brain-mode
    health.py                 # /health, /health/vendors, /recall/status, /gmail/status, /gemini-ears/status
    demo.py                   # /, /demo/*
    live.py                   # /live/*
    sessions.py               # /sessions/* (start, context, end, cancel, deliver, artifact, redeliver)
    meetings.py               # /meetings*, /ledger
    contract.py               # ws/{conversation_id}, /avatar/*, /webhooks/recall*, /realtime/recall-audio*
                              #   ⚠ the live-meeting contract — move LAST, or leave in main.py
    org.py                    # from org_api.py + org_avatars_api.py
    dashboard.py              # dashboard HTTP handlers (markup → frontend/templates); + dashboard_runtime_ui

  core/            ✅ done    # config, auth, crypto, security, entitlements
  persistence/     ✅ done    # store, billing  (+ org_avatars_pg)
  actions/         ✅ done    # actions(→workflow), action_deps, action_plane, action_reconcile,
                              #   autopilot, executor, ledger, outbox, outbox_pg, scheduler,
                              #   control_plane, approval_runtime_guard
  meeting/         ✅ done    # meeting_state, decision, emotion, end_of_turn
  brain/           ✅ done    # engine(brain), llm, rag, embeddings, tools, tool_registry
  integrations/    ✅ done    # recall_client, google_client, asana_client, JIRA_client, granola_client,
                              #   drive_client, anam_client, gemini_ears, GRAPHITI_client, tts,
                              #   gmail_watcher, vendor_health
  avatars/         🆕         # avatars, avatar_resolver, avatar_overlay
  runtime/         🆕         # runpod_runtime, gpu_runtime, native_runtime

  # existing upstream packages — keep, tidy internally
  browser/                    # browser automation (operator, dal, providers, router, planner, …) + browser_meeting
  cedric/                     # + cedric_mcp
  datafoundation/             # dal, router, sync, connectors, resolver
  demo_mvp/                   # northstar_provider, router, runtime
  knowledge/                  # dal, ingest, router  (+ graphiti_client could live here instead of integrations)
```

✅ = already implemented & verified on the fork branch `restructure/repo-structure`
(6 domain packages + 4 of the `api/` slices, every step 1310 tests green).
🆕 = new domains this proposal adds for upstream's newer modules.

## Rationale (why these seams)
- **`api/` is the biggest win.** `main.py` (6,720) is 90% route handlers; splitting
  them into routers under `api/` leaves `main.py` as thin app assembly. Do it
  easiest-first (pages/granola/oauth/avatars — done), then health/demo/live/sessions,
  and the **live-meeting contract handlers LAST and most carefully** (or never — the
  hard rule is not to break `ws/<conversation_id>` + recall webhooks).
- **Domains group by "what it talks to / what changes together":** vendor clients
  (`integrations/`), the action lifecycle (`actions/`), live-meeting understanding
  (`meeting/`), reasoning+retrieval (`brain/`), foundations (`core/`), storage
  (`persistence/`).
- **New domains** — upstream grew an avatar-identity cluster (`avatars/`) and a
  compute-runtime cluster (`runtime/`) worth isolating.
- **Existing packages stay** (`browser/ cedric/ datafoundation/ demo_mvp/ knowledge/`).

## How to land it safely (proven recipe)
1. Green baseline first (`pytest backend/tests` → 1310 passed), before and after every step.
2. **Moves-only, with compatibility shims** at each old path (`sys.modules` alias) so
   every importer keeps working with zero edits; one domain per verified commit.
3. Watch the three gotchas that each cost a debug cycle: `__file__`-relative paths
   (`config.REPO_ROOT`, `store.STORE_PATH` need a deeper `parents[N]`); module-vs-package
   name clashes (`actions.py`→`workflow`, `brain.py`→`engine`); and tests that call a
   handler/constant directly (retarget to the new module). See `BACKEND-RESTRUCTURE.md`.
4. For `api/` extractions: hoist genuinely-shared helpers to `api/deps.py` first, then
   move a route group + its group-only helpers; verify routes by **request**, not
   `app.routes` (this repo's FastAPI defers `include_router` via `_IncludedRouter`).

## Where to do this
Upstream `Dunic15/Laura` is the live tree and hasn't been restructured — that's where
this pays off. The fork branch `restructure/repo-structure` is a **working prototype**
of the ✅ rows (domain packages + first `api/` slices), useful as a reference/diff, but
it can't push (archived) and has diverged from upstream, so the clean path is to
**re-apply this on upstream in small verified PRs** using the recipe above.
```
```
