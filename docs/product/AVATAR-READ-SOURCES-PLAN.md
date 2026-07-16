# Avatar read sources — build plan

> Restored 2026-07-16 (the original was lost in the iCloud working-tree
> corruption). Statuses updated to reflect what is now built.

**Ownership rule:** Laura owns the tools natively; Cedric owns Slack.

```
LAURA (native, the avatar acts "by itself"):
   • book meetings  → Google Calendar   (native ✅)
   • read calendar  → upcoming_meetings (native ✅ 2026-07-16)
   • send emails    → Gmail             (native ✅)
   • read + update Notion               (build)
   • read Drive/Docs/uploads → RAG      (build — per-org seam ready)
CEDRIC:
   • Slack — all of it (delivery, approvals, connectors)
```

Rule: native for the core tools (Google + Notion); Cedric for Slack + any
long-tail tool later. Don't re-implement HubSpot/Linear/etc natively — that's
what Pipedream/Cedric is for. **Reading is always Laura-side** (feeds RAG,
must be fast on the live path) — never route reading through Cedric.

## Priority 0 — fix what's broken (trust, not nice-to-have)

- [x] **org_id split-brain** (`u_<hash>` vs uuid) — billing/artifact reads now
      degrade gracefully for personal orgs (2026-07-16); ghost `org_sff` seed
      removed (#244).
- [x] **Calendar auto-join uses the wrong org** — dispatch now attributes the
      meeting owner's org via organizer/attendee → connected-Google/registered-
      user match; Demo org only as fallback (2026-07-16). Gmail-invite path
      still pending (file owned by parallel session).
- [x] **Relay echo** (Gemini transcribes the avatar's own voice from mixed
      audio) — token-coverage echo gate in `_is_echo` (2026-07-16). Ears stay
      `off` in prod until the WS-relay host exists (App Runner blocks inbound WS).

## Priority 1 — prereqs for real reading (before ingest)

- [x] **Embeddings hash→local** (fastembed, key-free) — live in prod
      (`embedding_provider: local` in /health).
- [x] **Per-org RAG index isolation** — per-(org, avatar) index files next to
      the SQLite store, merged at retrieval (`rag.retrieve(org_id=...)`),
      threaded through the live answer path (2026-07-16). Grounding-threshold
      recalibration still open — do it when real docs arrive.

## Priority 2 — the read capability (the product jump)

- [x] **Calendar → session brief** *(scoped-down first read, shipped
      2026-07-16)*: the owner org's upcoming Google Calendar rides the
      memory_brief channel + `upcoming_meetings` brain tool (zero network
      live, bounded, cached, self-gates on the org's connected Google).
- [ ] **Drive → RAG:** user connects their own Drive folder from the dashboard
      → ingest into the per-org index (`rag.build_org_index`) → re-sync.
      Avatar answers from real Drive docs in-meeting. (Extends
      `drive_client.py` + the P1 seam above.)
- [ ] **Notion native (read + update):** one Laura Notion OAuth → ingest for
      reading AND write for "update Notion." Same integration does both.
- [ ] Generalize both behind one `connected_sources` abstraction + add file
      upload (zero-OAuth).

## Priority 3 — polish the action behavior

- [ ] Per-action **approve/auto policy**: auto for low-risk (internal Notion
      note, draft); quick 1-tap confirm for outward/irreversible (send email,
      book with external people). Native execution, just safe.
- [ ] **Live-lookup tool** (`lookup_knowledge`/`open_document`) for "read it
      right now" — off the hot path, hard timeout, defer fallback. Do after
      ingest proves out.
