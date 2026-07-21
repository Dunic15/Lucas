# Laura: Product Roadmap (consolidata, agg. 2026-07-14)

> Documento **decision-ready** per founder + advisor. Consolida e sostituisce come vista
> operativa: [`ROADMAP-2026-07-14.md`](ROADMAP-2026-07-14.md) (resta come storico del meeting),
> [`YC-COMPANIES-CATALOG.md`](YC-COMPANIES-CATALOG.md) (censimento verificato = fonte del moat),
> [`WEDGE.md`](WEDGE.md) (posizionamento), [`NATIVE-INTEGRATIONS-PLAN.md`](NATIVE-INTEGRATIONS-PLAN.md)
> (executor nativo), [`PHOTOREAL-DITTO-PLAN.md`](PHOTOREAL-DITTO-PLAN.md) (volto).
> Fonte: demo 14/07 con Christian Mischler + Jacopo Trinca + direttive owner.

---

## 1. Tesi di prodotto (il moat, in 4 righe)

**Vinciamo sulla verticale + l'esecuzione live delle azioni, NON sulla faccia né sull'automazione
generica dei meeting.** Il censimento YC verificato è netto: il **volto photoreal** è commodity
(Keyframe $0.06/min) e la **meeting-automation post-call** è già presa (Circleback). L'unico spazio
difendibile è possedere UN vertical e far **eseguire all'avatar, dentro la call, le azioni esatte di
quel vertical**: una lane dove il comportamento "avatar-nel-meeting-che-esegue" non è ancora occupato.

Corollario operativo (regge già l'architettura): il cervello è nostro, l'avatar è una bocca
comandabile e sostituibile. Il valore si sposta su **knowledge del cliente + template di processo +
azioni verticali eseguite**: non sul plumbing (Recall) né sulla faccia (Ditto/TalkingHead).

**Perché ORA (time-box).** La ricerca trend ([`YC-TRENDS-2022-2026.md`](YC-TRENDS-2022-2026.md), 3 passaggi
deep-research) cambia il ritmo: (a) la finestra si è aperta ott–nov 2024 (OpenAI Realtime + Recall Output
Media + MCP) ma gli **stessi rilasci armano i competitor**; (b) il whitespace **tiene all'intersezione esatta**
ma la versione **orizzontale chiude in ~12–24 mesi** (Zoom AI Companion 3.0 / Teams Facilitator), mentre la
**verticale profonda regge ~2–4 anni solo se verticalizziamo davvero**. Correzione: il **CRM/ATS write da solo
è ormai table-stakes** (Coffee/Attio/Gong), non il moat; il moat è la **combinazione** (esperto verticale che
parla × grounded × flag live × *profondità* del write). → **la scelta nicchia (§2) è time-boxed.**

---

## 2. La chiamata sulla nicchia (decisione owner: dato + raccomandazione, non scelta unilaterale)

Il catalogo verificato mette due lane sul tavolo. **La scelta resta di Duccio + Christian + Jacopo**;
qui c'è solo la sintesi data-backed e cosa comporta committare su UNA.

| | **Opzione A, Sales B2B consultivo** (whitespace #1) | **Opzione B, Recruiting via buyer agenzia/ATS** (validato #2) |
|---|---|---|
| Evidenza | **Nessun competitor confermato** fa "avatar-live-sulla-call + esegue" (Caretta refutato 0-3) | Domanda enorme e validata (Juicebox ~$10M ARR, Alex $17M) **ma** comportamento identico già occupato |
| Rischio | Domanda da provare (whitespace = anche "nessuno l'ha chiesto?") | Red-ocean sullo screening candidati (Alex/Lightscreen/Peoplebox) → entrare dal buyer staffing-firm/ATS |
| Buyer | Team sales (AE/SE), call discovery/demo piene di next-step; WTP alta, poca regolazione | Agenzie di staffing / ATS (non il candidate-screening) |
| Asset in-repo | Da costruire (knowledge + template) | Parziale (niche4 HR-Italy scoped, ma MeetingState italiano = lift pesante) |
| Vincolo owner (14/07) | ✅ nessuna nicchia compliance, WTP alta | ✅ ok se si evita il compliance e lo screening |

**Vincoli di decisione già fissati (14/07):** niente nicchia compliance; **UN solo use case validato**;
nicchia dove **gli umani costano tanto**; mission per-meeting tipo analyst/paralegal. Jacopo: **scrape
batch YC** per confermare la nicchia prima di committare.

**Cosa significa committare su UNA:** l'intero build item §3 (le 4-6 azioni) si tailora su quella
lane: 1 knowledge pack cliente, 1 template `process_templates/` con i `critical_gaps` di quel vertical,
1 connettore di sistema (CRM per Sales / ATS per Recruiting), 1 avatar tailored. **Non si sceglie una
faccia, si sceglie un set di azioni da possedere.**

> **Terza via pragmatica (ponte, non alternativa):** Founder/EA "clona te stesso" (`avatars/duccio`
> esiste) è la wedge a frizione zero che l'executor nativo §3 già serve; utile come **ambiente demo
> perfetto** e primo dogfood mentre si valida A vs B, non come nicchia commerciale finale.

---

## 3. Il set di azioni verticali da shippare (build items + seam esatti)

Le **4-6 azioni comuni** alle due lane (dal catalogo). Le prime tre esistono già come capability; il
lavoro vero è (c)/(d) via executor nativo, (e) come nuovo connettore niche-dependent, (f) come template.

| # | Azione | Stato | Seam |
|---|--------|-------|------|
| a | **Entra nella call** (Zoom/Meet/Teams) | ✅ esiste | Recall + calendar auto-join + `/join` + `POST /sessions/start` |
| b | **Risponde grounded** dai process/product doc del cliente | ✅ esiste, serve knowledge cliente | `brain.py` (RAG) + `avatars/<id>/knowledge/` (pack del cliente, non demo) |
| c | **Auto-schedula il next step** | 🔜 executor nativo (#199/#200/#205, flag OFF) | `executor.py::calendar.create_event` → `google_client.create_event()`; approvazione da `dashboard.py` |
| d | **Manda l'email** di recap/next-step | 🔜 executor nativo (stesso loop) | `executor.py::email.send` → `google_client.gmail_send()`; scope `gmail.send` |
| e | **Scrive nel CRM/ATS** (stage, note, owner) | ⛔ nuovo, dipende dalla nicchia | **NUOVO** connettore (HubSpot/Salesforce se Sales · ATS se Recruiting) sul modello `executor.py`; LATER, parte dopo la scelta nicchia |
| f | **Flagga una qualifica mancante** live (no budget owner / no decision date) | 🟡 estende MeetingState | `meeting_state.py` (`critical_gaps` del template) + intervento di chiusura in `decision.py`: **zero-latency, deterministico** |

Nota: (a)(b) sono l'infrastruttura orizzontale già bought/swappable; (c)(d) sono l'executor nativo
(sotto in NOW); (e)(f) sono la **parte verticale che diventa il moat** e si accende solo dopo la
scelta nicchia (§2).

---

## 4. NOW / NEXT / LATER

### NOW (questa settimana)

| # | Item | Owner | Note |
|---|------|-------|------|
| 1 | **Accendere il native executor (go-live)**: azioni (c)+(d). Crypto-gate ✅ **DONE** (Fernet at-rest + fail-closed key, draft PR #205). **Resta solo l'env-deploy:** set `GOOGLE_TOKEN_ENC_KEY` in SSM + flip `NATIVE_EXECUTOR=true` + consenso Google write (scope `calendar.events`+`gmail.send`) | Claude | Una sola env-deploy → **coordinare con la sessione Gemini** (l'env-deploy App Runner serializza) |
| 2 | **Photoreal Ditto. Fase 1** (il vero fix): ripristina immagine v3 sul pod `e7jwxnvi5fcf85`, verifica `/stream` reale, e2e in Meet → photoreal-che-parla 720p | Claude (infra) | ½ giornata; dire "Fase 1" per partire (pod ora spento) |
| 3 | **Connettori a due toggle indipendenti** (view Connections): «Aggiungi Google» (nativo → Calendar+Gmail write, alimenta l'executor, zero Cedric) + «Aggiungi Slack» (add-on via Cedric). Attivabili singolarmente o insieme | Claude | PR draft in corso; deploy dopo Gemini |
| 4 | **Etiquette "scusarsi entrando"** (estende first-call activation) | Claude | live-path: review attenta (latenza) |
| 5 | **Fix calendar-connect Cedric** (stale-read runtime serverless Vercel/Neon) | **Ben** | atomica #39 in main; resta il read runtime |

### NEXT (1–2 settimane)

| # | Item | Owner | Note |
|---|------|-------|------|
| 6 | **Cedric = add-on opzionale (toggle)**: `execution_mode = native \| cedric` come scelta utente esplicita (promuove il gate globale `SURFACE_WEBHOOK_URL`) | Claude | Seam: `config.py`, `dashboard.py` settings, branch in finalize/`cedric/integration.py`; org Cedric esistenti invariate |
| 7 | **OAuth per-org** (ogni utente il suo Google) + fix attribuzione auto-join (oggi → org di default) | Claude | Chiave `org_oauth` sul durable-org id del ledger; ⚠️ non introdurre un terzo id (split-brain `u_<hash>` vs uuid) |
| 8 | **Ricevute con provenance**: ogni "Done" con link vero (evento Calendar / thread Gmail) su ledger row + artifact; chiude il "Done" opaco di Cedric | Claude | Seam: `ledger.py` (status `approved`/`executed` + `provenance_url`/`provenance_ref`), `dashboard.py` |
| 9 | **Dashboard semplificata**: TAGLIA: riga cost-estimate, badge provider/model raw, colonne metadata non-azionabili. TIENI primario: lista meeting + coda approvazioni + status/receipt per-azione | Claude | Il job della dashboard diventa "approva ciò che Laura vuole fare, e vedi cosa ha fatto" |
| 10 | **Photoreal Ditto. Fase 2**: auto-wake zero-touch + tuning "ottimo 720p" + deploy #193 | Claude (infra) | — |
| 11 | **Intervention-mode admin** + **host-preview DM** (turn-taking come opzione admin, direttiva Christian) | Claude | live-path: review |
| 12 | **Demo account "setting perfetto"** (investor/YC). UN ambiente, UNA azione perfetta alla volta | Duccio + Claude | Founder/EA come ponte demo (§2) |
| 13 | **API-key self-serve** (bring-your-own key in un vault) | **Ben** + Claude | direttiva Christian: l'utente porta solo la key |

### LATER

| # | Item | Owner | Note |
|---|------|-------|------|
| 14 | **Scelta nicchia** ← §2 (Sales-whitespace vs Recruiting-validato) + scrape batch YC (Jacopo) → decisione Duccio/Christian/Jacopo | Duccio + Christian + Jacopo | Sblocca il 15 e l'azione (e) del §3 |
| 15 | **1 avatar tailored** sulla nicchia scelta; le azioni §3 diventano capability: knowledge pack cliente + template `critical_gaps` + connettore CRM/ATS (azione e) | Claude | Parte dopo il 14; è dove si costruisce il moat |
| 16 | **Connettore CRM/ATS** (azione e §3). HubSpot/Salesforce se Sales, ATS se Recruiting; modello `executor.py` | Claude | niche-dependent |
| 17 | **Photoreal Ditto. Fase 3**: web call ultra-HD/4K off-meeting (unico posto oltre i 720p) | Claude (infra) | — |
| 18 | **Email vere per-avatar** (multi-tenancy) | Claude | — |
| 19 | **GPT Live experiment**: A/B OpenAI Realtime (`gpt-realtime`) vs Gemini Live su voce+turn-taking; metrica: latenza, qualità turn-taking, €/min, se batte lo split Cerebras-brain + ElevenLabs-voce | Claude (dopo Gemini) | parte quando Gemini è a regime |

> **One-login: DEPRIORITIZZATO** (owner: di fatto già così, login Cedric una volta sola).

---

## 5. Brain & voice provider (contesto LLM)

- **Oggi in prod (live):** `BRAIN_PROVIDER=groq` + `GROQ_BASE=api.cerebras.ai` → Cerebras `gemma-4-31b`;
  post-meeting = Anthropic Sonnet 5.
- **In corso (sessione Duccio):** switch live a **Gemini** (Vertex/Flash). Codice provider pronto (#202).
  Env: `BRAIN_PROVIDER=gemini`, `BRAIN_MODEL_FAST=gemini-2.5-flash`, `GEMINI_API_KEY` in SSM.
  **Duccio deploya nella sua sessione** (env-deploy serializza. Claude sta fuori; vedi NOW #1 coord).

---

## 6. Guardrail (mai violare)

- **Contratto live-meeting intatto**: `ws/<id>` + SSE `/avatar/stream/<id>` + poll `/avatar/messages/<id>`,
  `{type:"speak", text}`, firme `recall_client`/`anam_client`. L'executor gira a finalize/approval, **mai**
  sul path transcript→speak.
- **Demo key-free intatta**: `NATIVE_EXECUTOR` off di default; niente key Google → solo badge dashboard;
  stub+hash offline mai richiedono una key.
- **Org Cedric invariate**: ogni deploy con `SURFACE_WEBHOOK_URL` resta Model A; il nativo è additivo e opt-in.
- **Transcript = PII**: in memoria + artifact store, mai nei log (guard hook). L'executor manda solo testo
  già distillato (owner/due/subject/body), mai transcript.
- **Crypto token OAuth → ✅ SODDISFATTO** dal draft PR #205 (Fernet at-rest + fail-closed key). Il go-live
  executor NON è più bloccato dalla crypto: resta solo il set `GOOGLE_TOKEN_ENC_KEY` in SSM (env-deploy).
  Migrazione a KMS resta un miglioramento LATER, non un gate.
- **Latenza è il prodotto** sul live path.

---

## 7. Cose per BEN

1. **Calendar-connect Cedric** (stale-read runtime Vercel/Neon): fix atomico #39 in main; resta il read runtime.
2. **API-key self-serve** in sicurezza (vault): NEXT #13.
3. **One login**: deprioritizzato; confermare la meccanica se lo riprendiamo.
4. Review PR Cedric #33–#40.
