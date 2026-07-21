# Laura: Roadmap di prodotto (documento strategico)

> **Cosa è questo file.** La roadmap *strategica* di Laura come startup, a passo
> aggressivo: l'individuo ottiene valore da solo (e può clonare sé stesso) → in un
> mese siamo enterprise-ready abbastanza da chiudere un primo deal. È un artefatto
> durevole, non una wishlist. Il Now/Next/Later operativo di GTM resta in
> [`roadmap.md`](roadmap.md). Fonti di verità che questo doc rispetta e NON riscrive:
> [`WEDGE.md`](WEDGE.md) (posizionamento),
> [`../ARCHITECTURE_CURRENT.md`](../ARCHITECTURE_CURRENT.md) (come funziona oggi),
> [`../infra/MULTI-TENANCY.md`](../infra/MULTI-TENANCY.md) (piano dati enterprise),
> [`../COMMERCIAL-ROADMAP.md`](../COMMERCIAL-ROADMAP.md) (lato Cedric/azioni).
> Ultimo aggiornamento: 2026-07-10.

---

## Il filo conduttore

Una sola tesi: **l'adozione bottom-up (personal + clone virale) genera il pull che
in poche settimane giustifica, e finanzia, l'enterprise.** Due fasi dopo la
baseline, a scala di settimane, ognuna deve *provare* qualcosa prima di sbloccare
la successiva.

| Fase | Finestra | Tema | Domanda che stiamo testando |
|---|---|---|---|
| **0: Baseline** | *fatto* | *Esiste e funziona live* | Un avatar sta in riunione, risponde grounded, consegna l'artifact? → **Provato** |
| **1: Personal Assistant (+ Clona te stesso)** | **settimane 1–2** (assistente base sett. 1, clone **fast-follow** sett. 2) | *L'individuo ottiene valore da solo, poi clona sé stesso* | Un singolo connette i suoi tool, chiama il suo avatar/clone e **torna**: e il clone fa iscrivere altri? |
| **2: Enterprise-ready** | **target ~30 giorni (fino a 45)** | *Un'azienda standardizza e paga in grande* | Multi-tenancy + SSO + audit + un primo avatar verticale (verticale da confermare): abbastanza per chiudere un primo deal? |
| **Orizzonte successivo** | **mesi 2–4** | *Difendibilità e scala* | Moat uncopyable + trust artifacts quando la domanda enterprise è reale |

Il moat non cambia mai fase: **ingestion della conoscenza + template di processo +
verticalizzazione**, con azioni provider-independent. Il plumbing (Recall, face,
TTS) resta commodity e swappabile; è un vincolo, non un asset.

---

## Fase 0, Baseline (FATTO, questo lo vendiamo già)

Onestà pre-revenue: siamo *pre-ricavi*, ma non pre-prodotto.

**Cosa è realmente fatto:**
- Backend (il *brain*) **LIVE** su AWS App Runner (eu-central-1), auto-deploy su `main`.
- Avatar in riunione via Recall (Zoom/Meet/Teams), face `/talk` open-source a
  **$0/min** in produzione; `/photoreal` MuseTalk costruito e validato e2e su stub.
- `MeetingState` (regex, zero latenza) → intervento di chiusura deterministico +
  `readiness_score` + artifact post-meeting.
- **Login Google live** (`backend/app/auth.py`), con il **seam `org_id == user_id`
  end-to-end** (sessioni + artifact stampati, `/dashboard` scoped per utente).
- **Dashboard v3** mergiata: connessioni personali dell'avatar, connected-to-tools,
  meeting past/upcoming.
- Pattern piattaforma "avatar = cartella": `avatars/laura`, `avatars/cedric`,
  `avatars/sff`, `avatars/duccio` già esistono.

**Cosa NON è fatto (non spacciarlo per fatto):**
- Multi-tenancy vera: **no** colonna `org_id` nello schema, **no** Postgres/RLS.
  Oggi SQLite **effimero** (si azzera a ogni deploy). `MULTI-TENANCY.md` è un
  *piano*, non codice.
- Knowledge base ancora ~3 doc demo per `laura`: l'utilità reale è gated
  sull'ingestion di processi veri.
- `/photoreal` su GPU reale **bloccato** su approvazione quota AWS.
- Login in *testing mode* (solo utenti test Google), non self-serve pubblico.
- Azioni: `actions.py` ha la *forma* provider-independent (SendGrid/Slack webhook,
  placeholder Notion/Jira) ma nessuna integrazione di produzione validata.

**Milestone che ci ha portati qui:** un avatar chiamabile che risponde grounded e
consegna l'artifact, in produzione. **Sbloccato.**

---

## Fase 1: Personal Assistant, con "Clona te stesso" come plus (settimane 1–2)

> **Tema: l'individuo ottiene valore da solo, self-serve; e la capability di punta
> è clonare sé stesso.** Il punto di partenza dell'arco, in ~2 settimane. Riduce il
> rischio più grande (*nessuno torna a usarlo*) e, col clone, accende il canale di
> acquisizione virale. **Un solo wizard**: crei il tuo assistente E, se vuoi, lo
> rendi un clone di te (voce+volto+stile).
>
> **Sequenza (deciso 2026-07-10):** settimana 1 = assistente base + retention;
> il clone è un **fast-follow in settimana 2** (prima proviamo che l'assistente
> tiene, poi accendiamo il moltiplicatore virale). **Fase 1 è gratis**: il clone è
> il vettore di acquisizione, non lo si monetizza; la fatturazione parte con
> l'enterprise (Fase 2).

**Obiettivo.** Un individuo si iscrive con Google, connette i *suoi* tool, dà al suo
avatar la *sua* conoscenza (o clona sé stesso), lo chiama in riunione e **torna**.
Zero handholding.

**Cosa costruiamo (il più piccolo insieme che prova il valore):**
1. **Self-serve reale**: uscire dal testing-mode Google (verifica OAuth, consent
   screen pubblico), signup → primo avatar in < 5 minuti.
2. **Avatar-creation wizard (unico, con modalità clone)**: l'utente crea il suo
   avatar dalla dashboard (nome, wake word, persona, carica knowledge) senza toccare
   il filesystem; una spunta "clona me" attiva voce+volto+stile personali. Sotto resta
   "avatar = cartella": il wizard *scrive* `avatars/<user>/avatar.yaml` +
   `knowledge/*.md`.
3. **Connessione tool personali per pain OAuth** (ordine da `laura-product-positioning`):
   **Calendar** (auto-join) + **Drive `drive.file`** (non-restricted) per primi;
   **Gmail restricted-scope** in coda (CASA/verifica). Slack va con la traccia Cedric.
4. **Memoria/knowledge personale**: ingestion self-serve dei doc dell'utente
   nell'indice RAG del suo avatar; carryover cross-meeting (ledger) scoped per utente.
5. **Nessun billing in Fase 1 (deciso: gratis)**: il Personal Assistant e il clone
   sono gratis per massimizzare il loop virale. Il meter di fine sessione resta e
   viene strumentato, ma la *fatturazione* (Stripe) si accende solo in Fase 2 sul lato
   enterprise. Niente `billing.py` da costruire ora; solo tracciare l'uso.

### Plus: "Clona te stesso" (capability + motore virale, dentro questa fase)

L'utente crea un avatar di SÉ STESSO (voce clonata + volto + stile/knowledge), lo fa
**presenziare/rispondere per lui**. Non è una fase separata: è la feature di punta del
Personal Assistant e, insieme, il growth loop.

**Growth loop:** (1) l'utente clona sé stesso col wizard; (2) il clone presenzia in
riunione / risponde in sua assenza → esposizione organica davanti a colleghi ed
esterni; (3) la disclosure "sono l'avatar di X" è anche la CTA implicita ("crea il
tuo"); (4) chi lo vede si iscrive e clona sé stesso → **il loop si chiude.**

**Perché credibile subito: riusiamo quasi tutto.**
- `/talk` (TalkingHead + `.glb`) già in prod a costo zero; voice cloning **ElevenLabs**
  già nel router TTS; `/photoreal` MuseTalk come upgrade di fedeltà (stage 2).
- **Esiste già `avatars/duccio/`**, clone reale dell'owner (CV+bio), che risponde in
  prima persona nella sua lingua; è letteralmente il precursore di questa feature.
- L'avatar personale entra in riunione con lo **stesso** contratto live di Laura -
  zero nuovo plumbing.

**Consenso / identità / deepfake; requisiti di prodotto, non opzioni:**
1. **Solo self-clone con prova d'identità**: cloni solo *te stesso*; consenso
   esplicito registrato; no upload di volti/voci di terzi. Gate legale non negoziabile.
2. **Disclosure "questo è un avatar": obbligatoria (deciso 2026-07-10)** all'ingresso
   in ogni riunione: non è opt-in. È il gate di consenso *e* il vettore virale (ogni
   presenza è un'impression con CTA implicita "crea il tuo"), quindi va sempre attiva.
3. **Confini di autorità**: il clone risponde grounded e dice quando non sa; **non**
   prende impegni commitment-class senza approvazione (aggancio alla Live Whisper
   Console dell'orizzonte successivo).
4. **Revoca in un click**: disattiva/cancella il clone (voce inclusa) quando vuole.

**Seam di codice toccati (fase):**
- `backend/app/auth.py` (uscita da testing-mode; `org_id == user_id` resta il seam).
- **Nuovo** router wizard + `backend/app/avatars.py` (scrittura folder/yaml lato server)
  + `frontend/dashboard.html`; step di consenso + modalità clone.
- `backend/scripts/ingest.py`, `avatars/<id>/knowledge/`, `backend/app/rag.py`,
  `backend/app/embeddings.py` (ingestion personale).
- `backend/app/tts.py` + `ELEVENLABS_VOICE_ID` per-avatar; `frontend/talk.html` /
  `photoreal.html` (volto per-avatar); riga di disclosure in `decision.py`/persona.
- `backend/app/drive_client.py`, calendar auto-join (webhook `sync_events` Recall).
- `backend/app/ledger.py` (carryover scoped per utente: vedi caveat Fase 2).
- Meter di fine sessione: solo **strumentazione dell'uso** (no `billing.py` in Fase 1;
  la fatturazione Stripe arriva in Fase 2, lato enterprise).

**Milestone che sblocca la Fase 2:** *utenti non-owner self-serve che tornano*
(retention W2 > 0, da misurare, non inventare) **+ K-factor del clone > 0** (i cloni
che presenziano generano iscrizioni tracciabili) **+ i primi team** dove più colleghi
hanno un avatar/clone; è il pull dal basso che giustifica la tenancy.

**Rischio principale.** Due volti: (a) il valore self-serve non regge senza ingestion
manuale (l'utente carica 3 PDF e l'avatar risponde male) → il wizard deve dare feedback
sulla qualità del grounding; (b) **abuso/deepfake e responsabilità** del clone; un
clone che dice la cosa sbagliata "come te" è danno reputazionale/legale → consenso,
disclosure e confini di autorità sono requisiti, non feature. Si lancia "clona te
stesso" controllato, mai "clona chiunque".

---

## Fase 2: Enterprise-ready (target ~30 giorni, fino a 45)

> **Tema: un'azienda standardizza e paga in grande.** A passo aggressivo: il target è
> **~30 giorni** (fino a 45 se il binding meeting→org slitta, vedi rischio (b)) per
> arrivare all'**enterprise-ready minimo credibile**, quanto basta a chiudere un primo
> deal, non lo standard SOC 2 completo. È un **target teso, non un impegno rigido**: se
> non si arriva, si taglia lo scope o si parte single-tenant-per-deal, non si allarga la
> finestra. Due sotto-tracce che si sbloccano insieme: **(A) multi-tenancy + SSO +
> audit** e **(B) un primo avatar verticale agganciato ai sistemi (quale verticale: da
> confermare)**. Gated su: i primi team emersi dal basso in Fase 1.
>
> **L'offerta enterprise è: soluzioni personalizzate per processi aziendali.** Non un
> prodotto uguale per tutti, ma un avatar configurato sul processo specifico del cliente
> (SOP, template, sistemi, terminologia); è esattamente ciò per cui l'azienda paga in
> grande, e ciò che rende il moat verticale difendibile.

**Scope onesto; cosa significa "enterprise-ready in ~30 giorni" e cosa NO.**

| ✅ Dentro i ~30 giorni (minimo che chiude un deal) | ⛔ Slitta all'orizzonte successivo |
|---|---|
| Postgres durevole dietro il seam DAO + Alembic | SOC 2 Type II |
| `org_id NOT NULL` su ogni tabella + scope `(org_id, meeting_key)` | Isolamento fisico DB/schema-per-tenant |
| RLS Postgres + `WHERE org_id` esplicito + **test di leak a 2 org in CI** | SCIM provisioning avanzato |
| SSO via **WorkOS** (SAML/OIDC), gratis fino alla prima connessione | **Scrittura live** nei sistemi durante la call |
| `audit_log` di base (append-only, solo metadati) + retention configurabile | Permission-aware retrieval / ACL doc-level |
| Ruoli minimi (owner/admin/member/billing) + admin console essenziale | Residency multi-regione reale |
| **1 avatar verticale** read-only + azioni post-meeting provider-independent (verticale da confermare) | Catalogo/marketplace di verticali |

### 2A: Multi-tenancy, SSO, ruoli, audit

**Obiettivo.** Trasformare il seam `org_id` (oggi `user_id`) in multi-tenancy reale
*senza riscrivere il prodotto*: proprio perché lo store è vuoto ed effimero *ora*, il
retrofit costa il minimo (tesi di `MULTI-TENANCY.md`). Questo è ciò che rende la
finestra di 1 mese realistica: non è un rewrite, è cablare un seam già previsto.

**Cosa costruiamo (in ordine, dal piano già scritto):**
1. **Postgres durevole dietro il seam DAO + Alembic** (`LAURA_DATABASE_URL` in
   `config.py`; Postgres se settato, altrimenti SQLite per il demo key-free). Risolve
   anche il bug dello stato effimero che azzera ledger/artifact a ogni deploy.
2. **`org_id NOT NULL` su ogni tabella**, stampato **una volta** a
   `POST /sessions/start` dal booker autenticato. **mai** dal nome di un partecipante
   (spoofabile). Scope di `meeting_key` per `(org_id, meeting_key)` (altrimenti due
   clienti sullo stesso link ricorrente fondono la memoria; e il `carryover_brief`
   finisce nel prompt LIVE).
3. **Isolamento a doppia cintura**: `WHERE org_id = ?` esplicito nel DAO + **RLS**
   Postgres come backstop. **Test di leak a due org in CI su Postgres reale**: unico
   punto in cui la tesi di sicurezza viene davvero esercitata. È la voce di scope
   *non comprimibile*: senza questo test non si vende a nessuna azienda.
4. **Skeleton identità + provisioning team**: `orgs`, `users`, `memberships`
   (owner/admin/member/billing), `org_domains`; admin console essenziale (chi ha quale
   avatar, chi paga).
5. **SSO / Google Workspace via WorkOS** (SAML/OIDC), gratis fino alla prima
   connessione, federato sulle tabelle sopra.
6. **Governance minima**: `audit_log` append-only (solo metadati, mai transcript),
   retention configurabile, one-pager sicurezza che confeziona il constraint
   memory-only-transcript che *già* rispettiamo (export/delete GDPR completi possono
   arrivare subito dopo, ma la one-pager serve per il deal).

**Seam di codice toccati:** `backend/app/store.py`, `ledger.py`, `config.py`, nuovo
`alembic/`, `main.py` (`start_session`), `org_api.py`, nuovo `backend/app/deps.py`
(risoluzione `org_id` server-side dal principal), `auth.py` (token→org). Tutti mappati
in `MULTI-TENANCY.md §4`.

### 2B: Un primo avatar verticale agganciato ai sistemi (verticale da confermare)

L'avatar smette di essere "una cartella con SOP sintetiche" e legge/scrive nei sistemi
reali; rispettando WEDGE (azioni provider-independent, guidate da `MeetingState` +
artifact, **non** bullonate ai tool-call del vendor). **Quale verticale sia il primo è
da confermare** (vedi decisione aperta): qui fissiamo il *come*, non il *quale*.

**Criterio di scelta.** Il dolore morde dove *"la persona che conosce il processo non è
in riunione"* e uno step saltato costa subito (denaro o rischio): lì stanno WTP e dato
strutturato più alti. Se ne valida **uno** a pilota pagato prima di aggiungerne un
secondo; e i successivi si aggiungono solo quando un cliente li tira.

**Esempi di avatar-di-processo su misura (illustrativi; il primo verticale è da
confermare).** Danno la forma della "soluzione personalizzata per processo":
- **RevOps / Sales (CRM)**: *deal-review avatar*: conosce stage-gate e campi
  obbligatori, segnala il deal senza security review o approvazione sconto, aggiorna il
  CRM a fine call. *Step saltato → deal fantasma, forecast sbagliato.*
- **HR / Recruiting (ATS)**: *interview-debrief avatar*: verifica che ogni intervistatore
  compili la scorecard, fa il bias-check, registra la decisione hire/no-hire.
  *Step saltato → assunzione sbagliata, gap di compliance.*
- **IT / Security (ticketing)**: *access & onboarding avatar*: percorre la checklist di
  sicurezza, segnala DPA o approvazione mancante, apre il ticket di provisioning.
  *Step saltato → escalation di sicurezza.*
- **Customer Success**: *kickoff / go-live avatar*: conosce la checklist di go-live,
  riporta ciò che è rimasto aperto dalla call precedente, redige il follow-up.
  *Step saltato → rollout in stallo, churn.*
- **Finance / Procurement**: *approval avatar*: conosce soglie di spesa e catena di
  sign-off, segnala l'acquisto senza approvazione. *Step saltato → spesa fuori budget, rischio audit.*
- **Ops / Compliance**: *process-audit avatar*: traccia live qualsiasi workflow ricorrente
  contro la sua SOP scritta e ne calcola la readiness. *Step saltato → processo rotto, nessun paper trail.*

**Nella finestra dei ~30 giorni ci fermiamo ai primi due gradini (i sicuri):**
1. **Read-only grounding**: l'avatar *legge* il record (deal, candidato, ticket…) e ne
   fonde lo stato in `MeetingState`/RAG ("questo record è allo stadio X, manca lo step
   Y"). Nessuna scrittura, nessun danno.
2. **Azioni provider-independent post-meeting**: artifact + MeetingState producono
   azioni astratte (es. `update_record`, `create_ticket`) che un adapter *traduce* nel
   vendor, best-effort e off-path. È `actions.py` esteso.
3. **Scrittura live nei sistemi**: **de-scoped** dalla finestra (vedi tabella): troppo
   rischio, troppo poca prova di valore, finché la domanda non è validata.

**Seam di codice toccati:** `avatars/<id>/process_templates/*.yaml` (nuovo template del
verticale scelto); `meeting_state.py` (vocabolario regex nuovi step);
`avatars/<id>/knowledge/` (pack verticale); **nuovo** `backend/app/connectors/`
dietro il seam di `actions.py` (adapter del sistema scelto, best-effort, off-path);
`autopilot.py` (gating con `readiness_score`/`critical_gaps`). Aggiungere un verticale
= template + knowledge + un adapter, **non** ricostruire il prodotto.

**Milestone che consolida la fase:** *primo team paga* su multi-tenancy con test di
leak verde + un avatar verticale read-only in un pilota. È il segnale che "un'azienda
compra".

**Rischio principale.** (a) I tempi: ~30 giorni (fino a 45) per tenancy+SSO+audit+verticale
è aggressivo; la mitigazione è che l'`org_id` è un seam già previsto (retrofit minimo su
store vuoto), non un rewrite, e lo scope è tagliato al "minimo che chiude un deal".
(b) Il **binding meeting→org**: oggi Cedric prenota con **una credenziale di servizio
condivisa** e l'auto-join da webhook non ha principal umano → se non risolto, ogni
sessione stampa l'org sbagliato = leak cross-tenant. Blocker cross-repo ("dipende da
Ben"), **da risolvere prima di popolare `org_id`.** (c) Fedeltà del grounding sui dati
reali del sistema (un consiglio sbagliato su un record vero brucia la fiducia).

---

## Orizzonte successivo: Platform / moat (mesi 2–4, demand-driven)

Solo dopo che la Fase 2 ha prodotto il primo deal.
- **Live Whisper Console**: supervisionare l'avatar *mentre parla* (approve-before-
  she-speaks solo per righe commitment-class, whisper-correct, mute/pause).
  *Uncopyable* per la latenza; completa i "confini di autorità" del clone.
- **Action Ledger + Readiness-Gated Autonomy**: action card con puntatore `because`
  alla decisione MeetingState che l'ha autorizzata (provenienza strutturata, niente
  transcript). `critical_gaps` → policy di approvazione, `readiness_score` → confidenza.
- **Verticali #2–#4** (HR, IT/Security, CS) + eventuale **scrittura live** nei sistemi
  una volta validata la domanda; catalogo di avatar verticali (content play).
- **Trust artifacts**: sub-processor page, DPA, security page presto; **SOC 2 Type II**
  + isolamento fisico per tenant whale/regolato quando la domanda enterprise è reale.
- **`/photoreal` GPU reale** sbloccata la quota AWS: face 720p per la presenza.

---

## Cosa NON facciamo ora, e perché (de-scope consapevole)

- **Clone Yourself come fase/prodotto separato**: è un *plus dentro* il Personal
  Assistant (stesso wizard), non una fase: separarlo raddoppierebbe la superficie
  senza aggiungere valore.
- **"Clona chiunque"**: solo self-clone con consenso; niente upload di volti/voci di
  terzi. È ciò che tiene il growth loop *sano* invece che tossico.
- **SOC 2 / isolamento fisico per tenant / SCIM avanzato**: fuori dalla finestra di 1
  mese; arrivano quando la domanda enterprise è reale, non prima del primo deal.
- **Scrittura live nei sistemi aziendali**: prima read-only + azioni post-meeting.
  Scrivere in un sistema di produzione a metà riunione è il massimo rischio con la
  minima prova di valore.
- **Ultra-HD / face off-Recall**: Recall cappa il live a 720p/15fps
  (`laura-recall-definition-ceiling`). Target "excellent 720p"; ultra-HD per marketing
  off-Recall.
- **Gmail restricted-scope self-serve**: CASA/verifica Google costosa; si differisce o
  testing-mode (<100 utenti). Calendar + Drive `drive.file` bastano per la Fase 1.
- **Self-hosting del meeting-bot come primario**: Recall è l'unico turnkey che streama
  un avatar animato come camera; crossover a milioni di min/mese. Teniamo Recall, con
  Attendee (self-host degradato) come fallback dietro `recall_client`.
- **Persistere i transcript**: teniamo l'esito (azioni + artifact), non la
  registrazione (constraint PII + superficie GDPR/consenso).

---

## Le scommesse strategiche (queste decidono se diventa una startup)

1. **La scommessa centrale: il PLG personal + clone virale genera in poche settimane
   il pull che sblocca l'enterprise.** È l'intero arco. Se gli individui non tornano e
   i cloni non fanno iscrivere altri (Fase 1), non c'è pull dal basso e l'enterprise
   (Fase 2) resta una vendita top-down in un red ocean. La milestone di Fase 1 (retention
   + K-factor + primi team) *deve* scattare prima di investire nella tenancy.
2. **Enterprise-ready in ~30 giorni è credibile solo perché non è un rewrite.** L'`org_id`
   è un seam già previsto su uno store vuoto ed effimero: la scommessa è che tenancy +
   SSO + audit + un verticale read-only bastino a chiudere un primo deal, rimandando
   SOC 2 / isolamento fisico / scrittura live. Se il "minimo credibile" non basta al
   buyer, si taglia lo scope o si parte single-tenant-per-deal; non si allarga la
   finestra in corsa.
3. **Il moat è il layer di processo, non il plumbing.** Accuratezza di grounding +
   template verticali + ingestion diventano *dipendenza*, mentre Recall/face/TTS restano
   commodity. Difesa di lungo periodo: profondità verticale + Live Whisper Console
   (uncopyable per la latenza).
4. **Clone Yourself è un moltiplicatore asimmetrico.** Fatti bene consenso/disclosure/
   autorità è il canale di acquisizione più economico; fatti male è un incidente deepfake
   che brucia il brand. Si lancia controllato o non si lancia.
5. **Il binding meeting→org (Cedric-principal) è il rischio tecnico che vale l'azienda.**
   Unica bug-class che chiude la società (leak cross-tenant). Va risolto, a livello di
   identità del booker, prima di popolare `org_id`. Dipende da Ben.

---

## Decisioni prese (owner, 2026-07-10)

1. **Clone = fast-follow settimana 2**, non giorno 1. Settimana 1 = assistente base +
   retention; settimana 2 = clone come moltiplicatore virale.
2. **Disclosure del clone = obbligatoria** (non opt-in): è il gate di consenso e il
   vettore virale insieme.
3. **Billing: Fase 1 gratis.** Il clone è il vettore di acquisizione, non lo si
   monetizza; il meter resta strumentato ma la fatturazione (Stripe) si accende solo
   in Fase 2, lato enterprise. Nessun `billing.py` da costruire ora.
4. **Finestra Fase 2 = target teso, non impegno rigido:** ~30 giorni, fino a 45 se il
   binding meeting→org slitta. Se non si arriva, si taglia lo scope o si parte
   single-tenant-per-deal; non si allarga la finestra.

## Decisione ancora aperta

- **Primo verticale della Fase 2: DA CONFERMARE (lasciato aperto di proposito).** La
  Fase 2 resta scritta in modo generico ("avatar verticali agganciati ai sistemi"): il
  *come* è fissato, il *quale* no. Candidati col criterio "chi ha WTP + dato strutturato +
  lo step saltato costa subito": RevOps/Sales (CRM), HR/Recruiting (ATS), IT/Security
  access, Customer Success. Da chiudere prima di scrivere il primo `process_template`: 
  non serve deciderlo ora.
