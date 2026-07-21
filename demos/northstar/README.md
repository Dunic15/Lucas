# Northstar: Laura MVP demo environment

An **isolated, deterministic, resettable** synthetic company for Laura's first
end-to-end demonstration. A fictional B2B SaaS company (**Northstar**) with one
fictional customer (**Acme Robotics**), a small controlled product to navigate,
a Company Brain knowledge pack, workflows, a machine-readable demo manifest, a
presenter script, and network-free tests.

> **Everything here is synthetic.** No real SFF, customer, employee, or
> credential data (a test enforces it). Nothing reads the clock or a random
> source; the world is byte-identical on every run.

This branch adds **only** `demos/northstar/**`. It does not touch Browser B0/B1,
`frontend/dashboard.html`, `talk.html`, the Action Control Plane, the avatar
runtime, migrations, or Cedric.

## What Laura demonstrates here

1. Understand Northstar from Company Brain documents.
2. Explain Acme's onboarding status and blocker.
3. Navigate a controlled product.
4. Find a target that needs **genuine visual perception** (not text/AX).
5. Propose one guarded, reversible operation.
6. Wait for canonical approval.
7. Verify the visible result after approval.

## Layout

```
demos/northstar/
  README.md                    this file
  knowledge/                   12 synthetic docs + knowledge_manifest.json
  workflows/                   5 deterministic workflow specs (.yaml)
  product/                     the controlled product (isolated FastAPI app)
  demo/                        demo_manifest.json + demo_script.md
  handoff/                     INTEGRATION-HANDOFF.md
  tests/                       network-free deterministic tests
```

## Run the product

```bash
# from the repo root
uvicorn demos.northstar.product:app --port 8971      # start
python -m demos.northstar.product.reset              # reset to seed
curl -s http://127.0.0.1:8971/healthz                # health check
```

Pages (predictable URLs, stable `data-testid`s):

| URL | Page |
|---|---|
| `/` | Home |
| `/customers` | Customers |
| `/customers/acme-robotics` | Acme account **(+ visual-only diagram)** |
| `/customers/acme-robotics/onboarding` | Onboarding checklist (accessible) |
| `/customers/acme-robotics/onboarding/{stage}` | Stage detail |
| `/customers/acme-robotics/implementation` | Implementation status |
| `/tasks` | Tasks (guarded-op UI) |
| `/security` | Security settings |
| `/healthz` · `POST /admin/reset` | health · reset |

## The guarded operation

`create_followup_task` → `POST /api/acme/tasks` (idempotency key
`acme-robotics:followup:data-integration-blocker`). Preview is
`POST /api/acme/tasks/preview` (**pure read, no write**). It previews before
writing, is idempotent (repeated approved execution → exactly one task),
rejection creates zero tasks, shows a visible confirmation, and `reset` restores
the initial state. Laura's approval system is **not** implemented here; the
product exposes only the controlled write the Action Control Plane will call.

## The visual-only fixture

On `/customers/acme-robotics`, the onboarding pipeline diagram has five nodes
that **all share the accessible label `"Onboarding stage"` and carry no
stage-name text**. The blocked stage is identifiable only by **fill (amber) +
position (second)**. Clicking it opens the Data Integration stage.
[`tests/test_visual_only.py`](tests/test_visual_only.py) proves text/AX
selection is ambiguous; a genuine visual read is required. The fully accessible
counterpart is the onboarding **checklist** page.

*Optional live check (needs a browser, not part of the network-free suite):*
serve the app, screenshot `/customers/acme-robotics`, and confirm a
colour/position read selects `#stage-integration` while a text/AX read cannot.

## Test

```bash
# from the repo root, network-free and deterministic
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest demos/northstar/tests -q
```

Covers: seed/reset, stable URLs+ids, the visual-only fixture, initial absence of
the follow-up task, no-write-on-preview, idempotent creation, visible
confirmation, knowledge-manifest integrity, demo-manifest schema, workflow
validity, and the absence of secrets / real data / nondeterminism.

## Integration

See [`handoff/INTEGRATION-HANDOFF.md`](handoff/INTEGRATION-HANDOFF.md) for
exactly what the later MVP integration branch must connect (ContextResolver
ingestion, avatar assignment, browser start URL, checkpoints, VisualPlanner
goal, guarded action, canonical approval, receipt, visual verification,
dashboard presentation). None of that is implemented on this branch.
