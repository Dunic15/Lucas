# Company Brain — enterprise knowledge data plane

Laura's **data plane**: securely ingest, index, and retrieve company knowledge
with grounded, cited, permission-correct answers. Strictly separate from the
**action plane** (OpenClaw + Pipedream) — Company Brain answers *"what does the
company know?"*; the action plane answers *"what should we do, and execute it
after approval?"*

Microsoft Graph is the first connector; the contracts are source-agnostic
(Google Drive, Slack, Notion, Confluence, Box slot in without touching the
retrieval core).

## Status

A **key-free vertical slice** built on the existing `backend/app/knowledge/`
package (M1), extended with M2: a source-agnostic connector contract, a fake
Microsoft Graph tenant, normalized ACLs with default-deny, durable delta
checkpoints, remote-deletion propagation, permission-only change tracking,
ACL-filtered hybrid retrieval, a read-only OpenClaw/meeting tool, connector
status + audit, and a webhook accelerator. **32 tests pass with zero API
keys** (14 required scenarios + resumable-sync, RLS proof, HTTP-surface, and
key-free inertness units); the full backend suite stays green (1342 passed, up
from the 1310 baseline). A five-lens adversarial review found and fixed one
critical + several high defects — see `RISKS-AND-MILESTONES.md`.

**Not enterprise-ready** until a real (synthetic, Laura-owned) Microsoft
tenant runs the same scenarios — see `RISKS-AND-MILESTONES.md`.

## Documents

| Doc | What it is |
|---|---|
| `ARCHITECTURE.md` | Architecture decision record (D1–D8) |
| `THREAT-MODEL.md` | Trust boundaries (TB1–TB5), assets, threats T1–T12, invariants |
| `CONTRACTS.md` | Canonical schema + the `SourceConnector` contract + retrieval API |
| `MSGRAPH-PERMISSIONS.md` | Capability→permission matrix, admin-consent flow, least-privilege story |
| `AWS-DEPLOYMENT.md` | Staged AWS proposal (Tier 0 pilot → Tier 2 enterprise) |
| `CONNECTOR-ROADMAP.md` | Staged GDrive / Slack / Notion / Confluence / Box plan |
| `RISKS-AND-MILESTONES.md` | Unresolved risks + smallest next production milestone (M3) |

## Code

| Path | Role |
|---|---|
| `backend/alembic/versions/0011_company_brain_graph.py` | Connector/ACL/sync-state/audit schema (FORCE-RLS, least-grant) |
| `backend/app/knowledge/connectors/base.py` | `SourceConnector` contract + typed records/exceptions |
| `backend/app/knowledge/connectors/msgraph.py` | Graph adapter (transport-injected) + prod transport skeleton |
| `backend/app/knowledge/connectors/fake_graph.py` | Synthetic Graph tenant + transport (key-free) |
| `backend/app/knowledge/sync.py` | Connector sync engine (delta, throttle, revocation, budget) |
| `backend/app/knowledge/datastore.py` | ACL/principal/identity/checkpoint/audit DAL + ACL-filtered candidate SQL |
| `backend/app/knowledge/retrieval.py` | Hybrid RRF retrieval + untrusted-content framing |
| `backend/app/knowledge/router.py` | `/org/knowledge/query|identity|status|revoke`, webhook (additive) |
| `backend/app/brain/tools.py` | `company_brain_search` read-only tool (additive) |

## Run the tests (zero keys)

```bash
GRAPHIFY_SKIP_HOOK=1 BRAIN_PROVIDER=stub EMBEDDING_PROVIDER=hash \
  .venv/bin/python -m pytest \
  backend/tests/test_company_brain_graph.py \
  backend/tests/test_company_brain_graph_pg.py -q -p no:cacheprovider
```

The `_pg` suite runs a real embedded Postgres (`pgserver`) as a
NOSUPERUSER/NOBYPASSRLS `laura_app` role with real migrations, so tenant
isolation is proven against actual row-level security, not mocks.

## Hard guarantees (each backed by a test)

1. Default deny — missing/stale/ambiguous ACL ⇒ never retrievable.
2. Tenant + ACL filter run **inside** the query, before any snippet leaves.
3. Tombstoned/revoked content is unretrievable before any purge.
4. No OAuth token/secret in canonical metadata, chunks, embeddings, or audit.
5. Retrieved text is inert data — cited, never executed; the tool is
   read-only by construction and cannot reach the action plane.
