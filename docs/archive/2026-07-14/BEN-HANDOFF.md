# Cosa deve fare Ben — handoff integrazioni (2026-07-14)

_Contesto: dopo la demo con Christian + Jacopo, Laura va **native-first** (l'utente porta solo la sua
LLM key; Cedric diventa un **add-on opzionale**, non più il core). Ben possiede Cedric (Next.js su Vercel)
+ Neon Postgres + il broker Pipedream. Questi sono i pezzi lato Cedric che restano a Ben._

## Messaggio pronto da mandare a Ben

> Ciao Ben — riepilogo di cosa resta lato Cedric/Vercel, in ordine di priorità:
>
> **1. Calendar-connect stale-read (il più importante).** Quando un utente collega Google Calendar via
> Pipedream, il toast dice "connesso" ma `/connectors` non lo riflette — è uno **stale-read serverless**
> (Vercel/Neon): la scrittura va a buon fine ma la lettura successiva torna il blob vecchio. L'upsert
> atomico (`upsertConnectorAtomic`, PR #39) è già in main; resta il **read a runtime** che deve leggere
> il blob appena committato (read-after-write / bypass cache). Finché non è fixato, nella dashboard il
> calendar non risulta mai "connected".
>
> **2. API-key self-serve in un vault.** Nodo sollevato da Christian: l'utente incolla la sua LLM key =
> rischio sicurezza. Serve un **vault** (bring-your-own-key) così le key non stanno in chiaro. È il
> blocco per l'onboarding self-serve.
>
> **3. Review + merge PR Cedric #33–#40** — sono i fix connettori (raw-blob read, anti-clobber,
> atomic upsert, roster-resolve, dedup `session.ended`). Alcuni già in main, gli altri aspettano te.
>
> **4. One-login** — deprioritizzato (di fatto già un solo login Cedric). Da confermare la meccanica
> solo se lo riprendiamo.
>
> ⚠️ **Nota deploy:** Cedric deploya da `feat/landing-page` (tuo branch), NON da main — quindi le fix
> vanno lì + tu possiedi le env Vercel (`VERCEL_TOKEN`). Se serve che io metta una fix, dimmi dove.
>
> Lato Laura (mio): il native executor Calendar/Gmail è già in prod dietro flag — quando accendo il
> Livello B, le azioni girano sul Google dell'utente **anche senza Slack**, quindi Cedric resta davvero
> opzionale.

## Perché serve (in breve, per te)
- Senza il fix #1, la dashboard non mostra mai il calendar "connected" anche quando Pipedream ce l'ha →
  è il bug che hai visto tu ("Tool connected" ma la UI non cambia).
- Il #2 sblocca il "porta la tua key" senza Cedric — la direzione decisa in demo.
- I native executor lato Laura rendono Cedric un add-on: le azioni vere (evento/mail) non dipendono più
  da Slack. Quindi il lavoro di Ben è **rendere pulito il connect**, non tenere in piedi il core.
