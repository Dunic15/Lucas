# Northstar Demo — Integration Handoff Contract

> What the later **MVP integration branch** must connect. Nothing in this list
> is implemented on `ananth/northstar-demo-company` — this branch ships only the
> isolated environment (knowledge pack, workflows, product, manifest, script,
> tests). The integration agent wires Laura's runtime to it.

Everything the integration needs is already pinned in
[`demo/demo_manifest.json`](../demo/demo_manifest.json) and the product routes;
this doc maps each Laura-side seam to the concrete thing it must read.

## 1 · ContextResolver ingestion (Company Brain)

- **Wire:** ingest `demos/northstar/knowledge/*.md` as one org's knowledge
  sources so retrieval can ground answers.
- **Seam (this base branch, do not modify here):**
  `backend/app/datafoundation/connectors.py` builds a SourceEnvelope per
  document; `backend/app/datafoundation/envelope.py::validate_envelope` defines
  the shape (`external_id`, `kind="document"`, `title`, `body_text`,
  `container_external_id`, `acl_mode`, `checksum`, …). Retrieval goes through
  the ONE boundary `backend/app/datafoundation/resolver.py::resolve(org_id,
  avatar_key, query, …)`.
- **Contract:** each knowledge source id in
  [`knowledge/knowledge_manifest.json`](../knowledge/knowledge_manifest.json)
  maps to one ingested document; the `expected_citations` in the demo manifest
  must be retrievable and cite the named headings.

## 2 · Avatar assignment

- **Wire:** assign avatar `laura` (manifest `selected_avatar`) to the demo org
  and give it the meeting goal `meeting_goal`.
- **Seam:** the org-avatar assignment surfaces on this base branch
  (`backend/app/org_avatars_api.py`, `avatar_overlay.py`). Not modified here.

## 3 · Browser starting URL

- **Wire:** point Laura's browser at `starting_url`
  (`http://127.0.0.1:8971/`) and restrict navigation to `allowed_domains`.
- **Product:** run `uvicorn demos.northstar.product:app --port 8971` (or import
  `demos.northstar.product.app:app`). Health at `/healthz`.

## 4 · BrowserObservation checkpoints

- **Wire:** map each `ordered_checkpoints[]` entry to a BrowserObservation
  assertion. Every checkpoint carries a `url` and (mostly) a `testid` — assert
  the element is present and shows the `expect_visible` state.
- **Determinism:** element ids/`data-testid`s are stable (see product tests);
  no timestamps or random ids appear in the DOM.

## 5 · VisualPlanner goal (the visual-only target)

- **Wire:** VisualPlanner must select the **blocked** onboarding stage from the
  diagram on `/customers/acme-robotics`. See `visual_only_fixture` in the demo
  manifest and [`tests/test_visual_only.py`](../tests/test_visual_only.py):
  all five nodes share the accessible label `"Onboarding stage"` and carry no
  stage-name text, so the choice is made on **fill (amber) + position (2nd)**.
- **Success:** clicking the correct node opens
  `/customers/acme-robotics/onboarding/integration`
  (`target_opens`). Text/AX-only selection is provably ambiguous.

## 6 · Guarded browser action

- **Wire:** the guarded action is `create_followup_task` →
  `POST /api/acme/tasks` with the stable idempotency key
  `acme-robotics:followup:data-integration-blocker`. Preview (no write) is
  `POST /api/acme/tasks/preview`.
- **Contract:** never call the create endpoint before canonical approval (§7).

## 7 · Canonical approval (Action Control Plane)

- **Wire:** route the decision through Laura's canonical approve door
  (`POST /org/actions/{action_id}/approve`) — **the Action Control Plane is out
  of scope for this branch and must not be modified here.** On the door's
  approval, and only then, call the product's guarded create.
- **Contract:** exactly one product write per approved action; rejection →
  zero writes. The product side is already idempotent, so a double-approve or
  retry is safe.

## 8 · Browser receipt

- **Wire:** capture the product receipt (`{status, task_id, idempotency_key,
  created_at}`) returned by `POST /api/acme/tasks` and surface it on the same
  provenance channel Laura already uses. Expected receipt is pinned in the demo
  manifest (`expected_receipt`: `task-0003`, created).

## 9 · Post-action visual verification

- **Wire:** after the write, re-observe `/tasks` and assert the new row
  `data-testid="task-task-0003"` and the confirmation banner
  `data-testid="task-confirmation"` (checkpoint `verify-visible-result`).

## 10 · Dashboard BrowserPresentation state

- **Wire:** reflect the browser session (current URL, last checkpoint, guarded
  action + receipt) into the dashboard's BrowserPresentation. **Do not modify
  `frontend/dashboard.html` on this branch.** The integration branch adds the
  presentation binding.

---

## Reset & repeatability

Between runs (and between integration test cases) reset the product:
`python -m demos.northstar.product.reset` or `POST /admin/reset`. State is
process-local and frozen-seeded, so integration tests can assume an identical
world each time.

## Explicitly NOT done on this branch

Browser B0/B1, `frontend/dashboard.html`, `talk.html`, Action Control Plane
code, avatar runtime, migrations, and Cedric are untouched. This branch adds
only `demos/northstar/**`.
