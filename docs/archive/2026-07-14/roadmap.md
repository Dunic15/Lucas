# Product roadmap: Now / Next / Later

> **Vista consolidata decision-ready (tesi moat + chiamata nicchia + NOW/NEXT/LATER, agg. 2026-07-14):**
> [`PRODUCT-ROADMAP.md`](PRODUCT-ROADMAP.md); è la fonte per l'artefatto one-page founder/advisor.
> **Roadmap strategica (arco a 2 fasi, passo aggressivo: Personal Assistant + Clona
> te stesso → Enterprise-ready in ~1 mese):** [`roadmap-startup.md`](roadmap-startup.md).
> Questo file resta il Now/Next/Later operativo di GTM.
>
> Living list. Each line names the code seam it touches. Kept in sync with
> `docs/product/WEDGE.md` (positioning) and `docs/ARCHITECTURE_CURRENT.md` (how it works).
> Last reviewed: 2026-07-09.

## Now
- **Native Google executor + Cedric-optional toggle (post-demo, 2026-07-14).** Laura executes its own
  approved actions, Google Calendar create-event + Gmail send, via Laura-owned OAuth; Cedric becomes
  an opt-in "power" add-on. Spec: `docs/product/NATIVE-INTEGRATIONS-PLAN.md`. Seams: `main.py` OAuth
  scopes + `/oauth/google/callback`, new `backend/app/executor.py` + `google_client.py`, `store.py`
  (org_oauth), `ledger.py` (status + provenance), `dashboard.py` approval queue, `config.py` toggle.
- **First cold-outreach signal test (SaaS, Niche 1).** Validate demand before more GTM
  spend. No code; feeds positioning/copy. Spec: `docs/gtm/outreach-experiment.md`.
  Reply content routes back here: repeated "process isn't written down" -> prioritize the
  ingestion-onboarding item below; repeated "push to HubSpot/Slack" -> raise actions item.

## Next
- **Design-partner knowledge ingestion (gated on a Batch-1 positive).** Turn one real SaaS
  onboarding process into a live template + knowledge pack; the true moat.
  Seams: `backend/scripts/ingest.py`, `avatars/<id>/knowledge/`, `avatars/<id>/process_templates/`.
- **Italian meeting tracking (gated on HR-Italy / Niche 4 interest).** Make the MeetingState
  wedge fire on Italian calls. Seams: Italian detection regex in
  `backend/app/meeting_state.py`, an Italian interview-debrief template in
  `avatars/<id>/process_templates/`, an Italian `avatars/<id>/knowledge/` pack. Not a folder
  drop; this is why HR-Italy is a *second*, not a first.

## Later
- **Provider-independent actions** (email/Slack/tasks driven by MeetingState + artifact) -
  raise priority only if outreach replies repeatedly ask for it. Per WEDGE.md, not bolted to
  vendor tool calls.
- **PII/security one-pager**: packaging of the memory-only transcript handling we already do
  (hard constraint 6), if buyers raise trust objections.
