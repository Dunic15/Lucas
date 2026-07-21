<!-- Generated 2026-07-12 by a code-grounded multi-agent audit (5 streams + synthesis).
Companion to MULTI-TENANCY.md (the decision doc). This is the IMPLEMENTATION plan:
ordered PRs with file:line, tagged robot/plug/person. Act on the PR sequence. -->

Grounding confirmed (hot-path drop `_persist_utterance(bot_id, ...)` store.py:423; `carryover_brief … WHERE meeting_key=?` no org filter ledger.py:263; `meeting_key` pure URL→code ledger.py:47; `sessions.org_id NOT NULL DEFAULT ''` store.py:319; config has only `laura_api_token`, no DB/demo-org; `auth_error` single global bearer integration.py:49). Synthesis below.

---

# Multi-tenancy Laura: piano di implementazione a tappe

> Sintesi di 5 stream di audit. **Agisci sulla Sequenza PR (§2).** Tutto è SQLite-first: la demo key-free resta byte-per-byte identica finché `LAURA_DATABASE_URL` è vuoto.

## 1. Modello (confermato, non si rilitiga)

```
orgs(id uuid PK, name, slug, plan, region, created_at)          ← il tenant. 1:1 con la workspace Slack di Cedric
users(user_id PK, email citext, name, picture)                  ← org_id ESCE dalla riga user
memberships(user_id, org_id, role)  role∈{owner,admin,member,billing}
org_domains(org_id, domain, verified_at)                        ← dominio verificato → "member"; consumer (gmail) MAI mappato
org_agents(org_id, avatar_id, alias, visibility, status) PK(org_id,avatar_id)  ← il GRANT org↔agente
org_tokens(token_hash → org_id, label)                          ← sostituisce il bearer globale unico
```

- **Agente = DEFINIZIONE condivisa** (cartella `avatars/<id>/`, persona/voce/volto, git-versionata) **+ GRANT per-org** (`org_agents`) **+ GROUNDING per-org** (`knowledge_chunks(org_id,avatar_id,…)` pgvector). Nessun fork di cartella per org, nessun `org_id` sulla dataclass `Avatar` (avatars.py:20); l'org arriva da `session.org_id`.
- **Privacy per-utente**: il transcript resta PII (memoria/DB, mai loggato). `member/guest` è **solo personalizzazione in lettura**; l'org di **scrittura** è vincolata al booking dal principal autenticato, mai dall'email di un partecipante (chiude lo spoof §3.1).
- **Il seam esiste già** (`Session.org_id` store.py:58, persistito store.py:319/401, filtrato `dashboard.visible()` dashboard.py:193). Oggi però `org == user_id` e **ogni booking macchina/calendar/Gmail stampa `org_id=""`**. Il lavoro è *formalizzare-e-riempire*, non da zero.

## 2. Sequenza PR

Legenda: 🤖 fattibile ora (DB vuoto/SQLite) · 🔌 serve Supabase/owner · 👤 serve Ben.

| # | Titolo | Cosa fa | File (con :line) | Stato |
|---|---|---|---|---|
| **1** | **Irreversible minimum: spine `org_id` + tabelle identità + Alembic** | Migrazione 0001 col DB vuoto: crea la spine identità (VUOTA) + `org_id NOT NULL` sulle 6 tabelle persistite + backfill `DEMO_ORG_ID`. Nessun cambio di comportamento (resta mono-tenant, tutto stampato Demo). **Dettaglio in §3.** | `config.py:277` (+`laura_database_url`,`demo_org_id`), `requirements.txt` (sqlalchemy/alembic/psycopg), `alembic/0001`, `store.py:256/268/280/286/292/319/488`, `ledger.py:66`, `store.py:58` | 🤖 |
| **2** | **Threading `org_id` nelle 3 scritture che lo perdono (hot path incluso)** | `_persist_utterance(org_id,bot_id,utt)` store.py:423 (call site store.py:215 `self.org_id`); `register_conversation(org_id,…)` store.py:543 (main.py:1028+2310); `save_artifact(org_id,…)` store.py:450 (main.py:1295). `_persist_session` ON CONFLICT(bot_id)→(org_id,bot_id) store.py:397. **Le utterances restano SQLite locale**: mai write di rete per riga. | `store.py:423/543/450/397/215`, `main.py:1028/2310/1295` | 🤖 |
| **3** | **Ledger retrofit. `org_id` arg obbligatorio su tutte le 7 funzioni** | Chiude i leak a più alto blast-radius: `carryover_brief` (finisce nel **prompt LIVE** ledger.py:263→main.py:2602) e la **DELETE di eviction** cross-org (perdita dati) `record_meeting` ledger.py:184. `meeting_key()` resta pura URL→code; isolamento via `WHERE org_id=? AND meeting_key=?`. `item_norm` diventa colonna → UNIQUE`(org_id,meeting_key,kind,item_norm)`. `resolve_item`/`resolve_by_action_id` prendono `org_id` nel WHERE. | `ledger.py:47/110/184/205/218/235/245/263`, callers `main.py:1301/1032/2600`, `autopilot.py:62/85` (service org) | 🤖 |
| **4** | **Sigilla le enumerazioni store-side** | `list_artifacts(org_id)` con `WHERE org_id=?` store.py:469 (consumers main.py:1462, org_api.py:29, dashboard.py:197); dedup di start `start_session` main.py:1084 → 409 generico senza `bot_id` estraneo quando `org` diverso; `all_sessions()` resta globale ma i consumer filtrano con `visible()`. `/meetings/list` main.py:1462 gate + filtro org. | `store.py:469/594`, `main.py:1462/1084`, `dashboard.py:197/211` | 🤖 |
| **5** | **`deps.py` resolver principal→org + `org_tokens` (fine del bearer globale)** | Un'unica dipendenza FastAPI con precedenza fissa: cookie user → token→org (registry `org_tokens`, sostituisce integration.py:49) → JWT Supabase claim `org_id` → **path anon = `DEMO_ORG_ID`**. Sblocca le 4 route `/org/*` (org_api.py:54/66/77/98) e `/ledger` main.py:1437 passando `principal.org_id` al ledger di PR3. Aggiunge `classify_participant()` (solo lettura). Registry come env-JSON ora; JWT dopo. | `deps.py` (new), `cedric/integration.py:49`, `auth.py:125/157/276`, `org_api.py:50-107`, `main.py:1098/1437` | 🔌 registry ora / JWT dopo · 👤 token per-workspace |
| **6** | **2-org leak test su Postgres reale (Testcontainers), RED-first** | CI su Postgres usa-e-getta: applica 0001 incl. RLS FORCE, connette come `laura_app` **non-superuser**, semina org A/B, `set_config('app.current_org',B,true)` per-txn, asserisce **zero righe A** su OGNI route di enumerazione (`/meetings/list`,`/org/actions`,`/org/search`,`/ledger`,sessions) + che una WHERE volutamente omessa non ritorna nulla (prova il backstop). **Non dipende dal Supabase dell'owner.** | nuovo job CI, `org_api.py:62/71`, `store.py:243` | 🤖 |
| **7** | **Control-plane Postgres + RLS + split `_connect`** | Due factory: `_hot_connect()` (sessions/utterances/conversation_routes = **sempre** SQLite/Litestream) e control-engine SQLAlchemy Core su `LAURA_DATABASE_URL` quando settato (else stesso file SQLite → demo invariata). `ENABLE`+`FORCE ROW LEVEL SECURITY` + policy `org_id=current_setting('app.current_org')::uuid`; `set_config(...,true)` per-txn corta, **mai** una txn lunga sul path SSE/answer. | `store.py:243/397/423`, `ledger.py:63/128`, `org_api.py:20` | 🔌 serve Supabase |
| **8** | **Agenti-per-org: `org_agents`, `list_for_org`, cache RAG per-tenant, `knowledge_chunks`** | `avatars.list_for_org(org_id)` (demo→`list_ids()` col filtro `hidden`; org reale→`org_agents`); **`rag._CACHE` chiave `(org_id,avatar.id)`** rag.py:256/339 (leak vivo appena due org condividono un id); `_load_pg`+`ingest_org_docs` su `knowledge_chunks` pgvector; dashboard roster+conteggi per org. | `avatars.py:159`, `rag.py:256/334/435/214`, `dashboard.py:79/233`, `main.py:183` | 🔌 pgvector |
| **9** | **SFF dogfood: org #1 reale** | Seed dati (vedi §4). | `avatars/sff/*`, seed script | 🔌 PR7/8 · 👤 Cedric-principal per booking reale |

## 3. La PRIMA PR, in dettaglio (pronta da scrivere)

**Obiettivo**: rendere ogni riga persistita tenant-owned, col DB vuoto, senza cambiare il comportamento mono-tenant. È l'unico momento in cui si può fare pulito (`ADD COLUMN NOT NULL` non passa su tabella non vuota senza backfill).

**Config** (`config.py:277`, vicino a `laura_api_token`):
```python
laura_database_url: str = ""                                   # vuoto = SQLite (demo key-free intatta)
demo_org_id: str = "00000000-0000-0000-0000-0000000000de"      # uuid Demo fisso = seed 0001
```
Cambia i default `""` → `settings.demo_org_id` su `Session.org_id` (store.py:58) e `store.create(org_id=…)` (store.py:488): niente più righe null-tenant.

**Alembic day-one**: aggiungi `sqlalchemy>=2.0`, `alembic>=1.13`, `psycopg[binary]>=3.1`; `alembic init backend/alembic`; `env.py` legge `settings.laura_database_url` e **no-op quando vuoto** (la demo SQLite non usa Alembic).

**Migrazione 0001** (un'unica revision, ordine obbligato):
1. `CREATE` spine identità VUOTA (`orgs`,`memberships`,`org_domains`,`org_agents`,`org_tokens`,`audit_log`); riservare `sso_connection_id`/`auth_method` per WorkOS/SCIM.
2. `INSERT` **org Demo** (`demo_org_id`): *prima* dei backfill perché le FK reggano.
3. Per ognuna delle 6 tabelle: `ADD COLUMN org_id uuid` (nullable) → `UPDATE` → `SET NOT NULL`. Backfill:
   - `sessions`,`users`: `'' → DEMO_ORG_ID`.
   - `utterances`,`conversation_routes`,`artifacts`: JOIN a `sessions` su `bot_id`, else `DEMO_ORG_ID`.
   - `ledger_items`: JOIN `sessions` su `bot_id` dove `bot_id<>''` (ledger.py:76), else `DEMO_ORG_ID`; **aggiungi colonna `item_norm`** popolata da `_norm(item)` (oggi calcolata solo in Python ledger.py:148-169).
   - `scheduled_events`: `DEMO_ORG_ID` (resta service-scoped; `event_id` globale unico).
4. Chiavi: `sessions` PK`(org_id,bot_id)`; `conversation_routes` PK`(org_id,conversation_id)` **+ `CREATE UNIQUE INDEX uq_conv_global ON conversation_routes(conversation_id)`** (la ws non autenticata deve risolvere l'org da solo `conversation_id`); `artifacts` PK`(org_id,bot_id)`; `ledger_items` PK`(org_id,id)`, UNIQUE`(org_id,meeting_key,kind,item_norm)`, idx`(org_id,meeting_key,status)`+`(org_id,action_id)`.
5. RLS `ENABLE`+`FORCE` + policy per tabella (attiva solo su Postgres; su SQLite è no-op; per questo esiste PR6).

**SQLite bootstrap** (`store.py:252-322`, `ledger.py:62-103`): specchia le stesse colonne `org_id`+`item_norm` nell'`IF NOT EXISTS` così i due schemi coincidono e il path `WHERE org_id=?` è esercitato anche in locale.

**Il 2-org leak test**: scaffolding qui (RED), asserzioni piene in PR6: seed org A/B su Postgres di test, principal=B, verifica 0 righe A da ogni route di enumerazione.

## 4. Agenti-per-org + SFF dogfood (senza rompere la demo)

**Seam** (PR8): la cartella resta la *classe* condivisa (`avatars.load()` invariato). L'ownership va relazionale:
- `avatars.list_for_org(org_id)`: `DEMO_ORG_ID`→`list_ids()` col filtro `hidden` piegato dentro (demo invariata); org reale→`SELECT avatar_id FROM org_agents WHERE org_id=? AND status='active'`.
- **RAG**: `rag._CACHE`/`_ABOUT_CACHE` chiave `(org_id,avatar.id)` (rag.py:256/339). **da fare nello stesso cambio che introduce la knowledge per-org, non dopo**: è un leak di retrieval vivo appena due org condividono `laura`. Branch demo legge `avatar.index_path` (hash-embed 512-dim, identico); branch org reale `_load_pg` su `knowledge_chunks` (pgvector `<=>` + rerank lessicale identico `_lexical_score` rag.py:392).

**SFF come org #1** (PR9, script di seed. **non** cancellare `avatars/sff/`, resta la sorgente + pack demo):
1. `INSERT orgs` (`'Swiss Founders Fund','sff','free'`), 1:1 con la workspace Slack SFF.
2. `INSERT org_domains('<sff>','sff.vc',now())`: **mai** `info@sff.vc`/domini consumer.
3. `INSERT org_agents`: `('<sff>','laura','Laura')`, `('<sff>','cedric','Cedric')`. `sff` **resta corpus, non terzo agente callable** (come il pack odierno avatars.py:76).
4. `ingest_org_docs('<sff>','laura', avatars/sff/knowledge/*.md)` e idem `cedric` → righe `knowledge_chunks` stampate `org_id=sff`. Da qui la Laura di SFF ricava **solo** da `knowledge_chunks WHERE org_id=sff`; la Laura demo resta su `avatars/laura/.index.json`, intatta.
5. `INSERT memberships(owner_user,'<sff>','owner')`.

Risultato: dashboard SFF mostra laura+cedric fondati sui doc SFF; la demo pubblica non cambia. **Warm lazy** per `(org_id,avatar)` alla prima sessione (il prebuild di boot main.py:183 va guardato ai soli folder demo).

## 5. Bloccato su

**🔌 OWNER: Supabase** (blocca PR7 in prod, non PR6):
- Progetto **eu-central-1**; ruolo **`laura_app` NON-superuser** (né `postgres` né `service_role`: entrambi bypassano RLS); estensioni `pgcrypto`(gen_random_uuid)+`citext`; `LAURA_DATABASE_URL` via **transaction pooler** in App Runner env/SSM, **mai in git** (vedi memory *exposed-secrets*).
- Il codice (PR1/5/7) atterra e resta su SQLite finché l'URL non arriva; la CI (PR6) usa il proprio Postgres e non aspetta.

**👤 BEN: Cedric-as-principal** (blocca lo stamping di org *reali* su tutti i path macchina/auto-join; fino ad allora pin `DEMO_ORG_ID`):
- **Richiesta esatta**: Cedric deve presentare un **bearer per-workspace** (una credenziale Laura per workspace Slack), **non** il token condiviso unico, e selezionare quello giusto per booking. Decisione confermata dagli audit: **token→org registry, NON org-nel-payload** (aggiungere `org_id` a `StartRequest` main.py:974 renderebbe l'org un request-param → viola §0/§3.1, e un bug di Cedric cross-prenoterebbe ogni tenant).
- **Handshake di onboarding**: quando una nuova workspace fa onboarding, chi conia la riga `orgs` + il token Laura? (endpoint self-serve Laura vs Ben chiama una provision API). Tracciato in `AGENT-CARD.md` open-q #4 / `MULTI-TENANCY.md` §5.1.
- I **due path request-less** (calendar webhook main.py:2301, Gmail watcher main.py:416) non hanno principal e girano sull'unico account Google di Laura → **intrinsecamente mono-tenant**: pin `DEMO_ORG_ID` e documentarli così. Per-org solo quando le connessioni calendar Recall diventano per-org (mappa `calendar_id→org`, il `calendar_id` è già nel payload main.py:2130).

## 6. Rischi da non sbagliare

1. **Org MAI derivata dal partecipante (spoof §3.1)**: l'email/dominio di chi entra in Zoom è non autenticato → può solo decidere `member/guest` in **lettura**. L'org di scrittura è vincolata al booking dal principal. `classify_participant()` non tocca mai `session.org_id` né scrive righe ledger.
2. **Hot-path fuori da Postgres**: `_persist_utterance` (store.py:423, chiamato per riga di transcript da add_utterance store.py:215) resta su `_hot_connect()` SQLite. Se il refactor control-plane lo instrada su Postgres, ogni riga aggiunge un round-trip di rete e rompe il contratto live (§3.4). Tienilo nel piano hot, non nel control.
3. **RLS `FORCE` + non-superuser + `set_config` per-txn**: RLS è inutile se l'app connette come `postgres`/`service_role` (bypassano) o se manca `FORCE` (anche il table-owner bypassa `ENABLE` semplice). E **mai** tenere `SET app.current_org` a livello sessione sul pooler transaction (GUC-leak: una connessione riusata serve il tenant precedente); usa `set_config('app.current_org',:org,true)` per txn corta; il path speak/answer non apre mai una txn lunga. **PR6 verifica entrambi**, non solo la prod.
4. **`meeting_key` cross-org merge**: `meeting_key()` deriva l'identità dal solo codice Meet/Zoom/Teams (ledger.py:47), quindi due org sullo stesso link ricorrente fondono la memoria; e `carryover_brief` la inietta nel **prompt LIVE** (ledger.py:263→main.py:2602). Deve diventare `(org_id, meeting_key)` **nella stessa migrazione** che scopa le letture, non dopo. Massimo blast-radius (arriva all'output parlato).
5. **`record_meeting` eviction DELETE = perdita dati cross-org** (ledger.py:184): peggio di una disclosure; su meeting_key condiviso conta e cancella righe di un'altra org. Prioritario in PR3.
6. **La demo prova SQLite dove RLS è no-op**: l'unico posto dove la tesi di isolamento è davvero esercitata è il **2-org leak test su Postgres reale** (PR6). Senza quel test in CI, le garanzie FORCE/non-superuser sono decorative. Scrivilo **RED-first** per guidare il retrofit.

**Conteggio choke-point confermato**: 5 fn `store.py` (`_persist_utterance`,`register_conversation`,`save_artifact`,`list_artifacts`,scoping `all_sessions`) + 7 fn `ledger.py` (`record_meeting`,`carryover_brief`,`open_by_meeting`,`search`,`items`,`resolve_item`,`resolve_by_action_id`) = **12**, come stimato dal doc.
