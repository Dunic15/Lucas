# HANDOFF: Sessione unica "finisci il prodotto"

_Scritto 2026-07-12 sera. Questo file è **il punto di partenza** per la singola
sessione Claude Code che, dopo aver chiuso tutte le altre, coordina e porta Laura
a "prodotto usabile da uno sconosciuto". Leggilo per primo, insieme a
`MEMORY.md`, `docs/product/roadmap-to-first-customer.md` e
`docs/product/COORDINATION-STATUS.md`._

---

## 0. START HERE (per la sessione coordinatrice)

1. **Leggi**: questo file + `MEMORY.md` (indice) + `roadmap-to-first-customer.md`.
2. **Regole d'oro** (non violarle): contratto live-meeting intatto; demo key-free
   (`stub`+`hash`); niente segreti in git (guard hook attivo); i transcript sono
   PII (solo memoria, mai log); **session-guard come step a sé** prima di ogni
   deploy (`GET /health` → `active_sessions==0` **e** App Runner non `IN_PROGRESS`);
   PR come `--draft`; niente autopilot (tutto passa da Cedric).
3. **Accessi che HAI già**: AWS (SSM/App Runner, pre-approvato), `gh` (Dunic15),
   Cedric staging (ricetta in `MEMORY.md` → laura-cedric-e2e-map), Slack `laura_ops`.
4. **Coordinamento**: `~/.claude/bin/codex-run "..."` (deleghi a Codex. **ORA
   BLOCCATO**: no credito OpenAI), `claude-run`/`claude-fan` (spawn sessioni Claude).
5. **Coda di lavoro**: la sezione §4 di questo file, in ordine. Fai i 🤖, fermati
   sui 👤/🌐 e segnala.

---

## 1. DOVE SIAMO ORA (stato verificato 2026-07-12)

| Sottosistema | Stato | Nota |
|---|---|---|
| **Backend Laura** | ✅ LIVE | `origin/main` #127 verde, App Runner `SUCCEEDED`, `active_sessions:0` |
| **Avatar deployati** | ✅ | cedric, duccio, laura, sff (health endpoint) |
| **Etichetta meeting** | ✅ LIVE | first-call activation, alzata di mano, opening grace, leave-on-command, turn-taking/deference, emozione |
| **Loop Laura↔Cedric (ricco)** | ✅ VERIFICATO oggi | dry-run staging: 3 proposte LLM (gmail_send+schedule_task+agent_task) + 3 memories → Anthropic OK |
| **Cedric prod** | ✅ LIVE | www.meet-cedric.com (deploy via `rm -rf .git`+token; vedi §5) |
| **Dashboard + Auth** | ✅ su main | Google Sign-In, gate private-beta (allow-list), dashboard v4 |
| **Credito Anthropic (Cedric)** | ✅ ricaricato | path proposte ricche sbloccato |
| **Credito Recall** | ✅ ricaricato | minuti meeting reali coperti |
| **Tooling coordinamento** | ✅ | codex-run, claude-run, claude-fan, vendor_health, accesso Slack |

**Non ancora mergiato / in volo:**
- `feat(llm): Cerebras first-class provider`: commit su `claude/dash-detail`, **da PR+merge**.
- **PR #128** (draft) `hand-raise motivation gate`: pushata da una sessione parallela
  (`9a0cd9dd`): budget/pacing/back-off/no-repeat sul hand-raise di #125. **496 test
  verdi + realism 100/100**. Tocca solo il blocco hand-raise (no overlap).
  **Review 2026-07-12: policy pura sound** (cap+pacing+back-off-se-ignorata; dedup Jaccard)
  → safe to merge (mark ready + merge, un solo deploy).
- **Track photoreal/emozione** (sessione `9a0cd9dd`, in corso): pod RunPod `yb0vao7ve33dsx`
  (A4500, v3+auth) per frame emotivi (happy/concerned/neutral) → cablaggio in live Meet.
  Root cause pull lento = registry ghcr privato (già risolta); eventuale redeploy su host
  più veloce. ⚠️ **pod acceso = billing per-minuto → terminarlo dopo il test.**

---

## 2. DOVE DOBBIAMO ARRIVARE (definizione di "prodotto")

> Uno **sconosciuto** si registra su lauravatar.com e usa da solo il loop completo:
> **login → avatar entra nel meeting → cattura azioni → esecuzione via Cedric →
> esito nella dashboard**, e tutto è configurato per **SFF Studio**.

Il **core funziona già** (dimostrato e2e con email vera + verificato oggi in dry-run).
Mancano soprattutto **gate esterni** e **hardening self-serve**.

---

## 3. ACCESSI: inventario completo

| Accesso | Stato | Chi lo sblocca |
|---|---|---|
| AWS (SSM, App Runner) | ✅ ho |. (pre-approvato) |
| GitHub `gh` (Dunic15) | ✅ ho | — |
| Cedric **staging** | ✅ ho | ricetta in memoria |
| Slack `laura_ops` bot | ✅ ho | — |
| Anthropic (Cedric) | ✅ credito |, (fatto) |
| Recall (minuti) | ✅ credito |, (fatto) |
| Cedric **prod** (Vercel) | ⚠️ indiretto | Duccio è VIEWER sul team di Ben → deploy via `rm -rf .git`+token (§5) o Ben |
| **Slack Interactivity URL** (bottone Approve) | ❌ non wired | 👤 Ben. App Cedric: prod `A0BD9SW7SRH`, staging `A0BGHED8Q4A`. **Muro = collaborator**: il nostro config token dà `no_permission` (verificato 2026-07-12, non è un problema di token/workspace). Ben o (A) setta l'Interactivity URL, o (B) **aggiunge duccio come collaborator** → poi lo settiamo noi via `apps.manifest.update` (converte 👤→🤖). |
| **App Slack Cedric distribuibile** | ❌ | 👤/🌐 owner app (Manage Distribution → Activate Public). Sbloccato anch'esso dall'opzione (B) collaborator. |
| **Google OAuth pubblico/Internal** | ❌ | 🌐 admin Workspace SFF, o publish+verifica Google |
| **OpenAI** (per Codex) | ❌ Quota exceeded | 👤 aggiungere credito su platform.openai.com |
| ElevenLabs/Cerebras/Groq/Runpod | ✅ chiavi in SSM | 👤+🤖 rotazione pendente |

---

## 4. CHECKLIST: cosa manca (in ordine)

Legenda: 🤖 = autonomo (lo fa la sessione) · 👤 = azione tua obbligata · 🌐 = gate esterno.

### Fase 1: chiudere il loop configurato (quasi fatto)
- [ ] 🤖 PR + merge **Cerebras first-class provider** (con session-guard + review).
- [ ] 🤖 Rivedere + mergiare **PR #128** (hand-raise motivation gate) se verde.
- [ ] 👤 **Slack Interactivity URL** su api.slack.com (fa scattare il bottone Approve). *Ti guido click-by-click.*
- [ ] 🤖/👤 **Promozione prod Cedric** (merge staging→main + `vercel --prod`); o Ben, o io col workflow §5.
- [x] ✅ Anthropic + Recall ricaricati.

### Fase 2: self-serve per un cliente esterno
- [ ] 🌐 **Google OAuth**: "Internal" nel Workspace SFF (serve admin) **oppure** publish "External" + verifica Google (giorni).
- [ ] 🌐/👤 **App Slack Cedric distribuibile** (Activate Public Distribution → link "Add to Slack").
- [ ] 🤖 **Test e2e su tenant vergine** (nuovo utente → Add-to-Slack → Gmail → dispatch → azione → esecuzione).

### Fase 3: pronto per SFF Studio
- [ ] 🤖 Dominio pulito **`app.lauravatar.com`** (App Runner custom domain + Cloudflare DNS).
- [ ] 👤+🤖 **Rotazione chiavi** esposte (OpenAI, Slack, Cerebras, ElevenLabs, Recall) → 🤖 aggiorno SSM.
- [ ] 🤖 Track **conversazione multi-persona** (turn-taking, roster, naturalezza; stream continuo).

### Fase 4: scala (dopo i primi utenti)
- [ ] 🤖 **Multi-tenancy vera** (Postgres/RLS al posto di SQLite+Litestream).
- [ ] 🤖 Osservabilità, rate-limit per-org, **billing reale**.

---

## 5. COSA DEVI FARE TU PER FORZA (👤: solo tu puoi)

1. **Slack Interactivity URL** (Fase 1): config app Cedric = Ben. Serve il suo click (o tu se hai accesso all'app). → io ti do i valori esatti.
2. **Credito OpenAI** (~$5–10) su platform.openai.com → sblocca Codex (Claude→Codex).
3. **Ruotare le chiavi** passate in chat (OpenAI, Slack) + storiche (Cerebras/ElevenLabs/Recall). Tu generi le nuove, **io aggiorno SSM**.
4. **Decisioni con terzi**: admin Google Workspace SFF (per OAuth Internal); owner dell'app Slack Cedric (per Distribution); eventuale `vercel --prod` se non vuoi darmi il token.
5. **Card @cedric "Approve"** in #test-laura (test fisico rimasto). *superato dalla dry-run, ma serve per il round-trip col bottone reale*.

---

## 6. COSA POSSIAMO TESTARE ADESSO (🤖: con i mezzi che abbiamo)

Tutto questo è fattibile **senza** aspettare gate esterni:

- ✅ **Loop ricco Cedric** (dry-run staging): *già fatto oggi, ripetibile su vari scenari*.
- 🤖 **e2e che INVIA davvero** su staging via `POST /api/laura/test/approve` (manda l'email reale); round-trip completo senza il bottone Slack.
- 🤖 **Suite backend** (476 test key-free) + **offline pipeline** (ingest+ask+simulate).
- 🤖 **Meeting reale** con Recall (ora c'è credito): entrare in un Google Meet, testare first-call/alzata-di-mano/leave/cattura azione dal vivo.
- 🤖 **Dashboard**: login demo (allow-list) → vedere avatar, dispatch, meeting, azioni, esiti.
- 🤖 **Scenari multi-persona**: far girare la naturalezza turn-taking su transcript sintetici.
- 🤖 **Cerebras/dominio/rotazione-SSM**: lavoro infra puro.

**Bloccati finché non arriva il gate:** login di uno sconosciuto NON in allow-list
(serve Google OAuth pubblico); "Add to Slack" sul Slack del cliente (serve app
distribuibile); bottone Approve su card Slack di staging (serve Interactivity URL).

---

## 7. Nota di rischio / meter safety
Ogni meeting reale accende il meter Recall per-minuto → **chiudere sempre la
sessione** a fine test. Prima di ogni deploy: session-guard come step a sé
(§0.2). Non deployare la prod di Ben senza consapevolezza. Non toccare la
demo key-free.
