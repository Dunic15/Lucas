<!-- Founder/PM session handoff; what shipped, demo-ready state, and the human TODO (owner + Ben). 2026-07-13. -->

# Laura: Founder session handoff (2026-07-13)

Obiettivo della sessione: portare il prodotto verso una **demo vendibile**, rivedere tutti i flussi, migliorare il multi-persona, organizzare tutto. Questo è il **resoconto finale**: cosa ho spedito, lo stato demo-ready, e **tutto ciò che resta a te (owner) e a Ben**.

---

## 1. Cosa ho spedito (tutto su `origin/main`, verde, deployato)

| PR | Cosa | Stato |
|---|---|---|
| **#132** | Sicurezza: `/live/token` (fatturava Anam senza auth) + `/gmail/status` (Meet-URL joinabili) → gattati | ✅ live, verificato 401 |
| **#140** | **Multi-tenancy `org_id` spine** (SQLite-first): ogni riga org-scoped, tabelle identità, Alembic 0001, isolamento 2-org provato | ✅ live, boot verificato |
| **#141** | Demo-ready: dashboard orientata all'**outcome** (ROI/ore-risparmiate + chip "delivered") + `readiness_score` reale + fix correttezza multi-persona | ✅ merged |

Più: precedenti #128/#129/#131 (hand-raise gate, Cerebras first-class, PII `/meetings/list` + grounding onesto).

**Nota di qualità:** la spine multi-tenancy è passata da una **review avversariale** che ha pescato un bug reale di meter-safety (utente loggato non poteva più fermare il meter). **fixato + test di regressione** prima del merge. Non l'avrei preso coi soli test.

**Docs prodotti:** `docs/product/DEMO-READY-ROADMAP.md` (roadmap 3 ondate), `docs/infra/MULTI-TENANCY-IMPLEMENTATION.md` (piano a step), `docs/gtm/PRICING-PITCH-ONEPAGER.md` (pricing + pitch).

---

## 2. Stato demo-ready (cosa mostra la demo OGGI)

**Gira e regge una demo (2-4 persone):** avatar in call reale, risposte grounded citate, artifact post-meeting con readiness, dashboard che ora mostra **outcome** (non solo note), loop Laura→Cedric→Slack #test-laura provato dal vivo. Sito pubblico live.

**Il momento "wow" multi-persona è affidabile SOLO se scriptato**: chi guida la demo deve rivolgere la domanda-chiave a **"Laura" per nome** (bypassa hand-raise/deference → risposta diretta). Senza, l'avatar resta silenzioso (design `first_call_required`).

---

## 3. 👤 TODO OWNER (Duccio): in ordine di leva

1. **Supabase** (sblocca Postgres/RLS + agenti-per-org): progetto eu-central-1, ruolo `laura_app` **non-superuser**, estensioni `pgcrypto`+`citext`, `LAURA_DATABASE_URL` in SSM (**mai in git**). Finché non c'è, il codice resta su SQLite (demo intatta).
2. **Google OAuth pubblico/Internal** (Workspace admin SFF) → uno **sconosciuto** può registrarsi. Oggi è allow-list private-beta.
3. **DNS `app.lauravatar.com`**: incolla i **3 record CNAME** (te li ho dati, Proxy OFF) → il dominio custom va live (già associato lato App Runner).
4. **Multi-persona: decisione prodotto** (io implemento appena decidi): (a) **cue di presenza** on-join vs (b) demo-mode con `opening_grace` auto. E: inquadra la demo su **2-4 persone**, con la domanda-chiave rivolta a "Laura" per nome.
5. **Pricing lock**: conferma i tier (Free/Pro $59/Business $149/Enterprise $2.5-5k) + il **surcharge Recall output-media** (unica incognita di costo) prima di metterli su sito/contratti. Vedi `PRICING-PITCH-ONEPAGER.md` §6.
6. **Rotazione chiavi esposte** (OpenAI/Slack/Cerebras/ElevenLabs/Recall): tu generi, **io aggiorno SSM**.
7. **Credito OpenAI** su platform.openai.com → sblocca Codex (parallelismo). Oggi è quota-exceeded.
8. **Invariante deploy:** `LAURA_API_TOKEN` settato su OGNI istanza pubblica (altrimenti il gate auth cade in "allow").

## 4. 🧑‍💻 TODO BEN

1. **Cedric-as-principal**: il blocco chiave: Cedric deve presentare un **bearer per-workspace** (non il token condiviso unico), così ogni sessione stampa l'org giusta. Decisione confermata dall'audit: **token→org registry, NON org nel payload** (spoof hole). Sblocca lo stamping org reale + il badge "Connect the brain".
2. **Slack Interactivity URL** sull'app Cedric → il bottone **Approve** funziona davvero.
3. **Wiring Gmail-send** per il workspace → l'azione email parte davvero (oggi "Connected" ma l'harness dà "Google not connected").
4. **Fix `laura_org_links`** su cedric-staging: `ensureSchema` non crea quella tabella → la risoluzione org via `getTeamIdByLauraOrg` fallisce.

---

## 5. Multi-persona: SPEDITO (#142), da calibrare dal vivo

Il tuning del parlato live multi-persona **è stato implementato e mergiato** (#142), non più deferito. Tre comportamenti, tutti dietro **manopole env con default conservativi**, passati da una **review avversariale che ha pescato 2 blocker reali** (over-firing + latenza su turno indirizzato) → fixati prima del merge:

1. **Single-interjection escape:** su un contributo grounded non-indirizzato ad alta confidenza + floor aperto, dice **una** riga invece della mano alzata silenziosa. **Condivide un solo budget sociale** con l'hand-raise (cap 4/meeting) → non può dominare.
2. **Deference 1.8→1.2s** (intervento non richiesto ~0.6s più veloce).
3. **Closing fallback** (idle+durata) oltre al regex, con guardia: mai su un turno indirizzato.

**Da fare TU (👤): un test dal vivo a 2-4 persone**, poi calibri via env senza deploy (tutte le soglie sono manopole):
- `HAND_RAISE_INTERJECT_WHEN_CONFIDENT` (bool, on) · `HAND_RAISE_INTERJECT_MIN_CONFIDENCE` (0.45) · `INTERJECT_MIN_COMPLETENESS` (0.6) · `INTERJECT_MIN_PAUSE_SECONDS` (1.0)
- `DEFERENCE_SECONDS` (1.2) · `CLOSING_FALLBACK_ENABLED` (on) · `CLOSING_FALLBACK_IDLE_SECONDS` (25) · `CLOSING_FALLBACK_MIN_MEETING_SECONDS` (180)

Se in call risulta troppo loquace: alza `HAND_RAISE_INTERJECT_MIN_CONFIDENCE` o metti `HAND_RAISE_INTERJECT_WHEN_CONFIDENT=false`. Se troppo timida: abbassa la confidence. **Nota:** con gli embedding hash key-free i punti grounded scorano ~0.42-0.53 (0.45 è tarato lì); con embedding semantici in prod si può alzare la soglia.

**Ancora deferito (dipende da terzi):** la **promozione prod Cedric** (deploy della prod di Ben) e l'**e2e email reale** (serve il connettore Gmail. Ben/owner).

---

## 5b. ⚠️ Item infra emerso dai log (rischio reale, per backend-infra/owner)

Durante i deploy ho visto nei log App Runner errori **Litestream ricorrenti** (ogni ~5 min):
`compaction failed ... non-contiguous transaction ids in input files`. Litestream è la replica `store.sqlite3 → S3`. Due implicazioni:
1. **Deploy flaky:** il restore-on-boot rallenta e a volte l'health-check di App Runner scade → **rollback** (è successo a #141: mergiato ma il 1° deploy è rollato, ri-triggerato). Non è un bug del codice (nessun crash Python nei log, test verdi).
2. **Rischio dati:** se il restore Litestream fallisse davvero, `ledger`/`artifacts` andrebbero persi a un deploy. **NON è un demo-blocker** (lo store è largamente effimero + la demo si resetta), quindi non l'ho toccato in autonomia; resettare un backup di prod è la tua chiamata.

**Remediation esatta (👤 owner/backend-infra; resetta la memoria cross-meeting corrotta):**
- **Opzione A (in place, distruttiva):** `aws s3 rm s3://laura-org-memory/store --recursive --region eu-central-1` → poi redeploy App Runner. Litestream riparte con una catena LTX pulita dal DB fresco.
- **Opzione B (non-distruttiva, più pulita):** cambia l'env App Runner `LITESTREAM_REPLICA_URL` da `s3://laura-org-memory/store` a `s3://laura-org-memory/store-v2` → redeploy. Catena pulita al nuovo path; i dati corrotti restano su S3 per forensics; nessun delete.
- Entrambe **resettano la memoria cross-meeting** attuale (accettabile: era corrotta). **L'argomento #1 per il Postgres di §3.1**: sposta il control-plane (ledger/artifacts) fuori da SQLite+Litestream, come da `MULTI-TENANCY.md`, e il problema sparisce alla radice.

## 6. Coordinamento

- La sessione parallela possiede **faccia/photoreal/emozione** (PR #130); non ho toccato quei file.
- **Codex** è bloccato (quota OpenAI) → ho orchestrato con Workflow/Agent nativi. Con credito, si aggiunge al fan-out.
- `origin/main` è **verde e autoritativo** (522 test); tutte le mie PR mergiate con session-guard, una alla volta, deploy verificati.
