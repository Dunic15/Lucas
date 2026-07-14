# Connections — piano integrazioni (Slack via Cedric · Google nativo · estendibile)

> Piano per l'area **Connessioni** della piattaforma: una pagina, ogni integrazione è una
> **card con toggle** (connetti/disconnetti, stato, account). Due famiglie oggi — **Slack
> (via Cedric, add-on)** e **Google Calendar+Gmail (nativo)** — con un registro pronto ad
> accogliere i prossimi connettori (CRM/ATS della nicchia, ecc.).
> Consolida il roadmap: NOW #3 (due-toggle), NEXT #6 (Cedric add-on toggle), NEXT #7 (OAuth
> per-org), LATER #16 (connettore CRM/ATS). Agg. 2026-07-14.

---

## 0. Il principio (una frase)

L'utente porta **solo la sua LLM key** (direttiva Christian: *"Cedric doesn't even exist — it's
just an LLM"*). Tutto il resto — Slack, Google, il CRM di domani — sono **connettori opzionali,
ognuno un toggle**, che accendono capacità. Slack passa da Cedric (l'add-on a pagamento di
Jacopo); Google è nativo dentro Laura; i due sono **indipendenti** (uno, l'altro, o entrambi).

---

## 1. Cosa ESISTE già (i seam — non partiamo da zero)

| Pezzo | Dove | Stato |
|---|---|---|
| Modello connessioni per-org | `store.set_connection()` + `store.connections_for_org(org_id)` (org_connections) | ✅ c'è |
| Vista Connections in dashboard | `dashboard.py` (`connections` + `org_connections` nel context) | ✅ c'è (da rifinire) |
| **Connect Slack (via Cedric)** | `GET /dashboard/connections/brain/slack/start` → `POST …/slack/complete` | ✅ flow c'è |
| **Catalogo connettori Cedric** | `GET /dashboard/connections/brain/connectors` → proxy `Cedric GET /api/laura/connectors` (connected / label / needs-reconnect) | ✅ proxy c'è |
| Disconnect | `POST /dashboard/connections/brain/disconnect` | ✅ c'è |
| **Connect Google (nativo, Calendar+Gmail)** | `GET /oauth/google/connect` → `/oauth/google/callback` (scope `calendar.events`+`gmail.send`, token cifrato Fernet #205) | ✅ c'è |
| **Toggle native \| cedric** | `config.execution_mode` (deriva da `native_executor`) | 🟡 stub (non è ancora scelta utente per-org) |

→ **Il lavoro è: unificare questi in UNA pagina Connections a card-toggle, rendere l'`execution_mode`
una scelta utente per-org, e aprire il registro ai prossimi connettori.**

---

## 2. L'architettura target (due famiglie + un registro)

### A) Slack — via **Cedric** (add-on opzionale, pay-tier)
- **Card "Connect Slack"** = il flow `brain/slack/start` → `…/slack/complete` (l'"Add to Slack").
- Una volta connesso, sotto la card compaiono i **connettori di Cedric** (dal proxy
  `/connectors`): stato per-connettore (connesso / etichetta account / serve riconnessione).
  Qui "finiscono tutte le integrazioni che passano da Cedric" (post recap su Slack, bottone
  Approve, ecc.).
- **`execution_mode = cedric`** → le azioni approvate vengono eseguite via Cedric→Slack.
- Owner del gate: **Ben** per l'Interactivity URL (fa scattare il bottone Approve reale).

### B) Google — **nativo** dentro Laura (Calendar + Gmail)
- **Card "Connect Google"** = `GET /oauth/google/connect` (scope `calendar.events` + `gmail.send`).
  Token refresh cifrato a riposo (Fernet, PR #205). Alimenta il **native executor**.
- Scelta UX: **una card "Google Workspace"** (un consenso, entrambi gli scope) è più pulita di due
  card separate Calendar/Gmail — Google chiede un solo consenso. Mostriamo però **due righe di
  capacità** sotto la card ("📅 Calendar · scrive eventi" / "✉️ Gmail · manda email") con lo stato,
  così l'utente vede cosa abilita. (Se in futuro serve granularità, si splitta.)
- **`execution_mode = native`** → Laura esegue Calendar/Gmail sul Google dell'utente, zero Cedric.

### C) Il toggle di esecuzione (chi esegue le azioni)
- **`execution_mode = native | cedric`**, **per-org**, scelta utente esplicita (oggi è derivato dal
  flag globale). Con **cedric** → approvazioni instradate a Cedric/Slack; con **native** → executor
  Google diretto. Default sicuro: com'è oggi (demo key-free intatta).

### D) Il registro connettori (estendibile — il pezzo che rende "tutto il resto" facile)
- Un **catalogo dichiarativo** dei connettori: `{id, nome, icona, famiglia (native|cedric),
  scope/capacità, connect_url, disconnect_url, status_source}`. La pagina Connections renderizza
  il catalogo → **aggiungere un connettore = una riga nel registro**, non una riscrittura.
- I connettori di Cedric arrivano **live** dal suo `/connectors`; quelli nativi (Google, e domani
  il CRM/ATS della nicchia) sono definiti nel registro Laura.

---

## 3. La pagina Connections (UX)

Una griglia di card. Ogni card:
- **Icona + nome** (Slack, Google Workspace, …) e **badge famiglia** ("via Cedric" / "nativo").
- **Pill di stato**: `Connesso` (verde) · `Serve riconnessione` (ambra) · `Non connesso` (grigio).
- **Etichetta account** quando connesso (es. `owner@azienda.com`, workspace Slack).
- **Bottone** Connetti / Disconnetti.
- Sotto: **righe capacità** (📅 Calendar scrive eventi · ✉️ Gmail manda email · 💬 Slack posta recap).

In cima alla pagina: il **toggle "Come esegue Laura le azioni"** (Nativo / Cedric) + una riga che
spiega cosa cambia. In coda: card **"Prossimamente"** per i connettori futuri (CRM/ATS, ecc.).

```
┌ Come esegue Laura ──────────────────────────────┐
│  ( ) Nativo (Google dell'utente)  (•) Cedric     │
└─────────────────────────────────────────────────┘
┌ Google Workspace · nativo ┐  ┌ Slack · via Cedric ┐  ┌ CRM/ATS · prossimamente ┐
│ ● Connesso  owner@az.com  │  │ ○ Non connesso     │  │ (dopo la scelta nicchia) │
│ 📅 Calendar  ✉️ Gmail      │  │ [ Connetti Slack ] │  │        [ soon ]          │
│ [ Disconnetti ]           │  │                    │  │                          │
└───────────────────────────┘  └────────────────────┘  └──────────────────────────┘
```

---

## 4. Fasi (in ordine di esecuzione)

### Fase 1 — Consolidare le due card-toggle (Google nativo + Slack/Cedric) — 🤖 codice, no deploy
- Render della pagina Connections come **griglia di card** dal registro (Google nativo + Slack/Cedric).
- Cablare stato/etichetta/disconnect già esistenti su ogni card; connect via i route esistenti.
- **Nessun deploy necessario** per il codice (render-side + config); il go-live vero è la Fase 4.

### Fase 2 — `execution_mode` come scelta utente per-org — 🤖 codice
- Promuovere lo stub globale a **preferenza per-org** (persisti su org_connections / settings).
- Toggle in cima alla pagina; branch in finalize/`cedric/integration.py` (già previsto: NEXT #6).
- Org Cedric esistenti **invariate** (default = comportamento attuale).

### Fase 3 — Rifinire il catalogo connettori di Cedric sotto la card Slack — 🤖 codice
- Renderizzare live il proxy `/connectors` (connesso / needs-reconnect / label) come sotto-righe
  della card Slack. "Finire tutte le integrazioni con quello" = mostrare e gestire ognuna.
- Self-heal link + honor team hint (già risolti lato Cedric #24).

### Fase 4 — Go-live esecuzione nativa (il gate) — 👤+🤖 UN env-deploy
- `GOOGLE_TOKEN_ENC_KEY` in SSM + `NATIVE_EXECUTOR=true` (coordinato con la sessione Gemini).
- Crypto già pronta (**#205**). → il toggle "Nativo" esegue davvero eventi/mail.

### Fase 5 — Registro pronto ai prossimi connettori — 🤖 codice
- **OAuth per-org** (NEXT #7): ogni utente il suo Google (oggi → org di default). ⚠️ non introdurre
  un terzo id (evita lo split-brain `u_<hash>` vs uuid).
- **Connettore CRM/ATS** (LATER #16) come nuova card nativa quando la nicchia è scelta
  (HubSpot/Salesforce se Sales · Ashby/Greenhouse/Bullhorn se Recruiting), sul modello `executor.py`.
- Ogni nuovo connettore = una entry nel registro + un handler tipo executor.

---

## 5. Dipendenze / gate umani
- **Ben:** Slack **Interactivity URL** (bottone Approve reale) · fix calendar-connect Cedric (stale-read).
- **Owner:** deploy Gemini + approvare il flip executor (Fase 4) · scelta nicchia (sblocca il connettore CRM/ATS).
- **Fatto:** crypto token OAuth (Fernet + fail-closed, PR #205).

## 6. Guardrail
- **Demo key-free intatta:** connettori OFF di default; nessuna key → solo card "Non connesso".
- **Org Cedric invariate:** `execution_mode` default = comportamento attuale; il nativo è additivo/opt-in.
- **PII:** transcript mai nei log; i connettori mandano solo testo distillato (owner/due/subject/body).
- **Contratto live-meeting intatto:** i connettori girano a finalize/approval, mai sul path transcript→speak.

---

## 7. Il "one-liner" per ogni pezzo che hai chiesto
- **Cedric = il connettore Slack, come toggle** → Fase 1+3 (card "Connect Slack" + catalogo `/connectors`).
- **Un altro toggle per Google Calendar (+Gmail)** → Fase 1 (card "Connect Google", scope già pronti).
- **Nelle Connections: Connect Slack / Connect Gmail / … + altri** → la pagina a card + il **registro** (Fase 1+5).
- **`execution_mode` chi esegue** → Fase 2 (toggle Nativo | Cedric per-org).

---

## 8. Coordinamento — 2 persone sul progetto

> Ora si lavora **in due**. `dashboard.py`, `config.py`, `store.py` sono **file caldi** (li toccano già
> #204 Gemini, #177 org_id, #166 billing, #153 self-serve, ecc.). Regole per non pestarsi i piedi.

**Regola d'oro deploy (App Runner serializza):** **UNA sola persona possiede i deploy App Runner** — in
questa fase la **sessione Gemini** (owner). L'altra persona lavora **fuori da App Runner** (solo codice +
branch + draft PR + `ssm put-parameter`). Prima di ogni merge su `main` o deploy: `aws apprunner
list-operations` su `laura-backend` (deploy in corso?) **+** `gh pr list` **+** `GET /health` →
`active_sessions == 0` (nessun meeting live). Mai battagliare un op in `IN_PROGRESS`.

**Split del lavoro Connections (indipendente per file → parallelizzabile):**

| Chi | Cosa | File (owner in questa finestra) | Deploy? |
|---|---|---|---|
| **Persona A** (owner) | Gemini merge (#204) · flip executor (Fase 4) · scelta nicchia | env App Runner + SSM | ✅ possiede i deploy |
| **Persona B** (codice) | Connections page (Fase 1) · `execution_mode` toggle (Fase 2) · render connettori Cedric (Fase 3) | `dashboard.py` + `config.py` (piccolo) | ❌ solo branch/draft PR |
| **Ben** | Slack Interactivity URL · fix calendar-connect Cedric | app Slack Cedric / Vercel | — |

**Ownership file (chi tocca cosa, per evitare conflitti):**
- **`dashboard.py`** = file caldissimo. Chi fa la pagina Connections lo **possiede** per quella finestra;
  l'altro si coordina prima di aprirlo. Fare la Connections **su un branch corto** e mergiarlo presto
  (meno tempo aperto = meno conflitti).
- **`config.py`** (execution_mode) = modifica piccola; coordinare in chat prima.
- **`cedric/`** = di chi fa l'integrazione Cedric.
- I PR Gemini (#204/#202) toccano `brain.py`/`llm.py` → **non** collidono con Connections. Ok in parallelo.

**Ordine di merge (sequenza pulita):**
`#204 Gemini` → `#205 crypto` → **flip executor (Fase 4, env-deploy)** → `Connections Fase 1` →
`execution_mode Fase 2` → `Cedric connettori Fase 3` → `OAuth per-org #7` → `connettore CRM/ATS #16`.

**Meccanica di coordinamento (già in repo):**
- **`docs/product/COORDINATION-STATUS.md`** = board vivo: chi lavora su cosa, PR in volo, delta non
  mergiati. **Aggiornarlo a ogni inizio/fine task** — è il canale async tra le due persone.
- **`origin/main` è la verità** (il main locale può essere stale — confronta sempre vs `origin/main`).
- **Draft PR di default** (l'owner marca ready) per evitare spam review; un branch per feature.
- **`gh pr list` prima di iniziare** un file caldo → vedi se qualcuno lo sta già toccando.

**Rischio di conflitto ORA (da sapere):** `dashboard.py` è toccato da più draft aperti — la Connections
va fatta **dopo** aver visto quali di quei draft mergiano prima (o rebase su `origin/main` appena mergiano).
Questo lo tiene tracciato `COORDINATION-STATUS.md`.
