<!-- Generated 2026-07-13 by a grounded unit-economics + positioning workflow. Numbers marked (owner) need confirmation before going on the site/contracts. -->

# Laura — One-Pager Commerciale

## 1. Pitch in una riga + il wedge

**"Laura è un'agente AI di processo che entra nelle tue call: le rende pronte prima, complete durante, azionabili dopo."**
*Non un notetaker. Non un avatar-giocattolo. Un'agente di processo che per caso ha una faccia — si unisce alla call, risponde citando i tuoi documenti, segnala lo step che stai per saltare, e trasforma le decisioni in azioni reali.*

**Il wedge (l'unico lavoro che i notetaker gratis e l'AI nativa di Zoom NON possono fare):**
**intervento live grounded + provenienza delle azioni auditabile.** I notetaker agiscono *dopo* la call — riassumono un buco già avvenuto. Laura sta *dentro* la call col processo in mano e chiude il loop in 3 momenti: **prima** (readiness), **durante** (traccia step/decisioni/owner a latenza zero e risponde citando), **dopo** (artifact = decisioni + owner + step mancanti + readiness score + azioni eseguite via Cedric, ognuna con puntatore alla decisione che l'ha autorizzata, nessun transcript salvato).
Difendibilità (in ordine): tracciamento processo → prevenzione step mancante → readiness score → esecuzione follow-up. La faccia e il bot Recall **non** sono il moat.

---

## 2. Chi compra e perché (ICP)

**Beachhead (compra per primo): SaaS B2B, 10-300 dipendenti, con funzione Customer Success / Implementation.**
Titoli: CS Manager, Onboarding/Implementation Specialist, Head of CS. Buyer economico: **Head of CS / Implementation / RevOps** (possiede il numero del go-live).

**Perché loro, concretamente:**
1. Il dolore è **datato al fatturato** — uno step saltato al kickoff (security review, provisioning accessi, DPA, owner non assegnato) blocca il go-live e ritarda il ricavo. È un numero che già sentono.
2. I **template già spediti** (customer_onboarding, implementation_access) calzano out-of-the-box — è config, non rebuild.
3. Alto volume ripetuto della **stessa forma di meeting** = la condizione esatta in cui un layer di processo compone valore.

**Espansione (in ordine):** consulenza/digital-transformation → agenzie/client-services → HR/recruiting Italia.
**Escludi:** >1.000 dipendenti; nessun workflow ricorrente meeting-heavy; **"utenti Otter/Fireflies felici che vogliono solo la trascrizione"** — se vogliono solo un transcript, Laura è il prodotto sbagliato e perdi sul prezzo.

---

## 3. Cosa compra (packaging) + tier con prezzo e margine

**Unità di valore:** un *avatar* (teammate di processo grounded su un knowledge pack) × un *seat* (chi la dispaccia) × *meeting-minute* (tempo live misurato) + il **Cedric action loop** come layer di outcome.
Asse di pricing: **per-seat, con monte-ore incluso pooled + overage a consumo** — l'unità che il buyer già mette a budget (Gong/Fireflies), disaccoppiata dal costo volatile di Recall.

| Tier | Prezzo | Incluso | Cosa aggiunge | Margine lordo |
|---|---|---|---|---|
| **Free** | $0 | 1 avatar, 90 min/mese, solo artifact | Acquisizione (costa ~$1,23/utente/mese → è **CAC**, non gratis-da-servire) | — |
| **Pro** | **$59/seat/mese** | 10 ore/mese pooled | Q&A live grounded + readiness score + flag step mancante + follow-up in bozza; overage $5/ora | **~86%** (costo ~$8,20 a 10h) |
| **Business** | **$149/seat/mese** | 15 ore/mese pooled | Full Cedric action loop (Slack recap + approval card + email + follow-up con provenance), dashboard team, template library per vertical, latenza prioritaria; overage $4/ora; **+$25/seat photoreal opzionale** | **~92%** (costo ~$12,30 a 15h) |
| **Enterprise** | **da $2.500–5.000/mese** (org, pooled) | monte-ore custom (~100h) | Tenancy dedicata (Postgres/RLS), SSO/SCIM, provenance audit-grade, SLA | **~92–96%** |

**Costo reale:** ~**$0,40–0,42 per meeting da 30 min** (~$0,82/ora), di cui Recall è il 75–85%. LLM/voce/AWS sono rumore. È un calo ~10x dalla vecchia faccia Anram (~$4,00).
**Per il primo cliente (SFF Studio):** atterra Enterprise al **floor $2.500**, non $5k, per ridurre l'attrito del primo deal; poi espandi su seat + ore. Fattura annuale con premio ~20% sul mensile.

---

## 4. Perché pagare invece del notetaker gratis (objection-killer)

**Obiezione:** *"Zoom AI Companion / Otter free mi riassume già le call — perché pagare?"*
**Risposta secca:** *"Un riassunto ti dice cosa ti sei perso. Laura ti impedisce di perderlo."*

Tre tagli concreti:
1. **Timing** — i tool gratis agiscono *dopo*, quando il DPA è già non confermato e il go-live è già slittato di una settimana. Laura lo segnala al minuto 0:29, **prima che tutti chiudano**. Il valore non è il recap, è **lo slittamento evitato**.
2. **Grounding** — un notetaker sa cosa è stato *detto*; Laura sa cosa il tuo *processo richiede* vs cosa è realmente successo, perché è grounded sui tuoi doc, e risponde live nella stanza.
3. **Esecuzione + audit** — i tool gratis ti danno testo da ri-digitare; Laura ti dà decisioni + owner + step mancanti + readiness score, **ed esegue** il follow-up con trail di provenienza e **nessun transcript salvato** (vantaggio GDPR, non solo feature).

**L'ancora di prezzo:** un solo go-live salvato vale più di un anno di seat. A $59 (86% margine) o $149 (92%) non stai confrontando Laura col "gratis" — confronti $149/mese col **costo di una escalation security o una settimana di ricavo slittato**. Ancora sempre lì, mai su "un Otter migliore".

---

## 5. La narrazione demo in 3 beat che chiude la vendita

Stanza a **2-4 persone** (dove i comportamenti multi-persona sono nitidi e sicuri — **mai 5+**).

- **Beat 1 — È viva e nella stanza (durante):** un umano guarda in camera e chiede *per nome* — *"Laura, abbiamo confermato il DPA su questo account?"* Chiamarla per nome bypassa hand-raise/deference: risponde diretta, veloce, grounded e citata. La reazione che stai comprando: *"aspetta, è intervenuta ed era GIUSTA"* — il momento che un notetaker strutturalmente non può produrre.
- **Beat 2 — Cattura lo slip (payoff):** in chiusura dice una volta sola: *"Prima di chiudere — la security review non ha ancora un owner. Readiness 72/100."* A schermo: score + lista step mancanti. Qui passa da "chatbot sveglio" a "ci ha appena salvato una settimana di go-live".
- **Beat 3 — È stato davvero fatto (dopo + provenance):** taglio sulla dashboard. Email follow-up = *inviata*, Slack recap = *consegnato*, approval card = *approvata*, ognuna con un "perché" che punta alla decisione — e **zero transcript**. **Chiusura:** *"Un notetaker ti avrebbe detto la settimana dopo cosa ti sei perso. Laura l'ha catturato nella stanza e chiuso prima che ti disconnettessi."*

**Da bloccare prima di mostrarla a freddo:** il trigger by-name di Beat 1 va scriptato (è la via garantita); **Beat 3 è il gap reale attuale** — serve seminare un artifact flagship con loop visibilmente completato + confermare che Gmail-send + Slack Interactivity URL funzionino. Senza Beat 3 completo la storia è "sveglia", non "vendibile".

---

## 6. Numeri da confermare (owner)

- **Surcharge Recall Output-Media** (la maggiore incognita di costo): il modello assume che Recall streammi la camera di Laura al rate base bot ($0,50/ora). Se c'è un premio streaming (+50–100%, plausibile), il costo/ora sale a ~$1,1–1,3 e i margini dei tier calano ~5–8 punti (comunque >78% su Pro). **Confermare con Recall prima di qualsiasi contratto cliente che citi un margine.**
- **Monte-ore Enterprise incluso:** nessun benchmark pubblico. I ~100h → 92–96% margine sono un'assunzione. Prezzo e margine del tier oscillano interamente su questo numero.
- **Prezzi non ancora owner-locked:** Free $0 / Pro $59 / Business $149 / Enterprise da $2.500–5k sono portati dal brief nel cost-model, da bloccare prima di metterli sul sito.
- **Cedric action loop non completamente live:** Slack Interactivity URL (bottone Approve) + wiring Gmail-send sono blocchi aperti (owner/Ben). È il differenziatore del tier Business e la dipendenza di Beat 3 — decidere se gatare Business come "action loop" finché non è affidabile, o mostrare un loop già seminato.
- **Cue di presenza al join** (product call aperta): presence cue vs opening_grace demo-mode, così l'avatar non sembra "morto" finché non chiamato — impatta quanto è indulgente Beat 1 con uno sconosciuto alla guida.
- **Frankfurt App Runner rate** assunto = tier US/Irlanda (muove la riga AWS <1 cent; safe internamente, re-verificare prima di un board deck).

*Fonti: docs/finance/cost-model.md, docs/product/WEDGE.md, docs/product/DEMO-READY-ROADMAP.md, docs/gtm/target-accounts-clay-brief.md, docs/product/roadmap-to-first-customer.md.*
