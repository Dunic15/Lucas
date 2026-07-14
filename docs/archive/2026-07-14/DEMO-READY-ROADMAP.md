<!-- Generated 2026-07-13 by a founder-level demo-readiness audit (5 dims + synthesis). -->

# Road to a Sellable Demo — Laura

## 1. Verdetto (3 righe)

Oggi la demo è **"rough-but-ok" su tutti i fronti**: l'ingegneria è forte ma il valore non arriva all'occhio dello sconosciuto che la guida. Due cose la fanno sembrare rotta o innocua a un buyer: un avatar **inerte finché non lo chiami per nome** e una dashboard che prova che Laura *ha preso appunti* ma mai che *ha fatto qualcosa*. C'è anche una falla che brucia soldi e fa fallire la security review nei primi 5 minuti (`/live/token` non autenticato, fattura Anam a $0.12/min).

**Cosa #1 da fare:** far scattare in modo affidabile **il momento wow multi-persona** (Laura che interviene con il fatto giusto) e **chiudere il loop provenance captured→delivered** in dashboard — sono i due beat che trasformano "carino" in "vendibile". Parto subito dai fix S ad alta leva mentre aspetto il merge multi-tenancy per toccare store/ledger/main.

---

## 2. Onde di lavoro (ordinate per leva = impatto-vendita ÷ effort)

### 🔴 ORA — ci agisco adesso (autonomo, alta leva, no faccia/photoreal, no store/ledger/main)

| # | Item | Sev | Owner | Eff |
|---|------|-----|-------|-----|
| 1 | Gate `/live/token` con `auth.gate` (o eliminarlo se Anam è legacy) — chiude il buco billabile aperto | blocker | 🤖 | S |
| 2 | Script demo: la domanda-chiave è rivolta a **"Laura"** per nome → bypassa hand-raise/deference/cooldown, risponde diretta = il wow multi-persona scatta senza codice | major | 🤖+👤 | S |
| 3 | Cue di presenza one-time all'ingresso ("ci sono, chiamami per nome") → l'avatar non sembra morto allo sconosciuto | major | 🤖 | S |
| 4 | `readiness_score` reale in `/demo/post_meeting` + seed → colonna e tile "Avg readiness" smettono di essere "—" | major | 🤖 | S |
| 5 | Seed **1 artifact flagship** su SOP onboarding/access (summary ricco, 3-4 azioni con owner, readiness alta, follow-up consegnato) come top row | major | 🤖 | S |
| 6 | Tile ROI orientato al valore ("Follow-up hours automated" / "Email inviate + meeting schedulati") ancorato a tempo/€ | major | 🤖 | S |
| 7 | Gate/strip `/gmail/status` — oggi leak PII (Meet URL joinabile + bot_id) | major | 🤖 | S |
| 8 | Middleware security headers (HSTS, X-Frame-Options DENY, nosniff, CSP minima) | polish | 🤖 | S |

### 🟡 PROSSIMA — subito dopo l'onda Ora (o dopo il merge multi-tenancy)

| # | Item | Sev | Owner | Eff |
|---|------|-----|-------|-----|
| 9 | **Provenance loop captured→delivered**: badge delivery per meeting + colonna "Delivered" (Recap→Slack ✓ · Email→sent · Approved). Render in `dashboard.py` ora; il carry del dispatch Cedric su `session.ended` dopo il merge main | blocker | 🤖 | M |
| 10 | Escape single-interjection ad alta confidenza dall'hand-raise: su pausa naturale + alta confidenza, **una** riga grounded parlata invece della mano alzata | major | 🤖 | M |
| 11 | Deference: sovrapporre l'attesa 1.8s alla generazione (stream durante lo sleep, cancella se parla un umano) o trim a ~1.2s → intervento non più "lento" (~3-4s) | major | 🤖 | M |
| 12 | Fallback trigger chiusura (N sec dall'ultima riga sostanziale + soglia durata) oltre al regex `detect_closing` → wrap-up e quiet-nudge scattano anche senza frase esatta | major | 🤖 | M |
| 13 | Rate limiting per-IP su `/demo/*`, `/tts`, `/live/ask` (cap più duro su `/demo/post_meeting`, Sonnet-5 per call) | major | 🤖 | M |
| 14 | Calendar Upcoming: se connesso, mostrare eventi reali + quale avatar auto-joina (via mock/read-only), CTA connect solo se sconnesso | major | 🤖 | M |
| 15 | Sito: hero riscritto su **before/during/after + readiness score** + one-pager pricing/wedge pubblicato | major | 🤖 | M |

### 🟢 POI — backlog / bloccato su merge / umani

| # | Item | Sev | Owner | Eff |
|---|------|-----|-------|-----|
| 16 | `/webhooks/recall`: token random per-sessione nell'URL realtime (bot_id non più segreto condiviso) — sensibile al contratto live | major | 🤖 | M |
| 17 | `present_names()` con roster transcript-merged (tocca `store.py` → **attendo merge tenancy**) | polish | 🤖 | S |
| 18 | Zero-state pulito per nuova org + tighten `visible()` (dopo che la spine SQLite tenancy atterra) | polish | 🤖 | M |
| 19 | Pannello Knowledge per-avatar con upload SOP (anche stubbed) — "è il TUO prodotto" | major | 👤 | L |
| 20 | Naturalezza a 5+ persone (round-robin, floor-pass multipli) — backlog, no per la demo | polish | 👤 | S |
| 21 | Split label "Gmail (watch)" vs "Gmail (send)" + Approve = "Setup needed" finché non wired | polish | 🧑‍💻 | S |
| 22 | One-pager security (data-flow, PII memory-only, vendor list, roadmap SOC2/WAF) | polish | 👤 | M |

---

## 3. Cosa faccio IO subito (🤖) — in ordine, in autonomia

Vincoli rispettati: **non tocco faccia/photoreal** (avatar-session), **non tocco store/ledger/main** finché il multi-tenancy non è mergiato (uso solo render-side e endpoint/config sicuri).

1. **Gate `/live/token`** (item 1) — è la falla billabile aperta, prima cosa. Verifico che ritorni 401 prima di qualsiasi call Anam.
2. **Gate/strip `/gmail/status`** (item 7) — chiudo il leak PII nello stesso passaggio auth.
3. **Middleware security headers** (item 8) — piccolo, chiude il grosso della prima occhiata di security.
4. **`readiness_score` reale + seed** (item 4) — rianimo colonna e tile.
5. **Artifact flagship onboarding SOP** (item 5) — seed top-row che racconta il prodotto, non il founder che debugga.
6. **Tile ROI a valore** (item 6) — relabel "drafted"→outcome, aggancio un numero a tempo/€.
7. **Cue di presenza on-join + riga script "chiamami per nome"** (item 3 + 2) — sblocca sia il "sembra morta" sia il wow multi-persona a costo ~zero.
8. **Provenance render captured→delivered lato `dashboard.py`** (item 9, parte render) — pronto per quando il carry su `session.ended` sarà mergiabile.

Poi apro PR draft (default repo) per l'onda Prossima (10-15), a partire da deference-overlap e escape single-interjection perché reggono il momento multi-persona.

**Nota di coordinamento:** items 9 (carry dispatch), 17, 18 restano in attesa perché toccano `main`/`store.py` che il multi-tenancy sta cambiando — non li apro finché non confermo il merge (evito conflitti sulla spine).

---

## 4. Blocchi umani (secco)

**👤 Duccio (owner):**
- **Product call:** cue di presenza vs demo-mode con `opening_grace` (45s) auto-attivante — decidi quale (io implemento la config).
- **Framing demo:** confermi il sweet spot **2-4 persone** (dove i comportamenti brillano) invece di puntare a 5+.
- **Deploy invariant:** setti `LAURA_API_TOKEN` su OGNI istanza non-prod pubblica (altrimenti il gate cade in "allow" con creds reali).
- **OK sui numeri pricing** e sul nuovo hero prima che vada sul sito.
- Priorità del pannello Knowledge upload (L) e del one-pager security.

**🧑‍💻 Ben:**
- **Merge della spine multi-tenancy (SQLite/store/ledger/main)** — è ciò che sblocca items 9/17/18. Dammi la conferma quando è su origin/main.
- **Slack Interactivity URL** per far funzionare davvero il bottone Approve.
- **Wiring del send path Gmail** (oggi "Connected" ma non invia).

---

## 5. Multi-persona — il piano per farlo impressionante

Priorità esplicita. L'ingegneria c'è già (attribuzione via nomi Recall, fuzzy-match con guardie, vocative gate, barge-in filler-aware, hand-raise che renderizza sul tile). Il problema è che **il momento wow è doppio-gated**: inerte finché non chiamata + ogni contributo non-indirizzato diventa una mano alzata silenziosa che il pubblico deve notare-e-invitare (timeout 120s → punto perso).

**Percorso affidabile al wow, in 3 mosse:**

1. **Ora (S, zero codice):** copione demo in cui un umano rivolge la domanda-chiave a **"Laura"** per nome. Il path by-name salta hand-raise, deference e cooldown → risponde diretta e puntuale. È il modo garantito perché il beat scatti davanti a uno sconosciuto.

2. **Prossima (M, codice):** **escape single-interjection ad alta confidenza** — quando il contributo streamed è ad alta confidenza E c'è una pausa naturale (nessun partial in volo), Laura dice **una** riga grounded invece di alzare la mano. Hand-raise resta il default per i punti a bassa confidenza. Così il "wow, è saltata dentro" scatta anche senza che qualcuno la chiami.

3. **Prossima (M, codice):** **deference sovrapposta alla generazione** (stream durante lo sleep 1.8s, cancella se parla un umano) o trim a 1.2s → l'intervento non-richiesto non atterra più a ~3-4s (che legge "lenta") ma quasi in tempo reale.

**A supporto (Prossima):** fallback di chiusura (tempo dall'ultima riga sostanziale) così wrap-up intervention e quiet-participant nudge — i due beat di facilitazione più impressionanti — non dipendono dal regex esatto.

**Differenziatori da mettere in scena** (nessun altro meeting-AI li fa a voce): saluto per nome dei joiner, un nudge al partecipante silenzioso al wrap-up, la mano alzata visibile sul tile per i punti a bassa confidenza.

**Fuori scope per la demo:** naturalezza a 5+ persone (round-robin, floor-pass multipli, soglie che scalano con l'headcount) → backlog. Inquadra la demo sul **2-4 persone** dove i comportamenti sono nitidi e sicuri.
