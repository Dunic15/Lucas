# Prompt da incollare in un'ALTRA sessione — ricerca YC esaustiva (2022–2026)

_Serve a tenere questa sessione sul prodotto/roadmap e far girare la ricerca altrove. Copia tutto il
blocco sotto in una nuova sessione Claude Code nel repo Laura. È self-contained._

---

Fai una ricerca ESAUSTIVA e verificata delle aziende finanziate da Y Combinator, **batch 2022→2026**
(W22, S22, W23, S23, W24, S24/Fall24, W25, Spring25/S25, X25, W26, Spring26 — includi i più recenti che
trovi), organizzata **per verticale di servizio**, per decidere in quale nicchia specializzare un
"process avatar".

**L'avatar** = un agente fotorealistico che ENTRA nei meeting live del cliente (Zoom/Meet/Teams),
risponde grounded dai process doc del cliente, ed ESEGUE le azioni di follow-up concordate (schedula su
calendar, manda email, scrive su CRM/ATS/PM tool).

**Per OGNI verticale di servizio** — Sales B2B consultivo, Recruiting/Staffing, Consulting/agenzie AI,
Insurance (brokerage+underwriting), Wealth/advisory finanziario, Legal/paralegal, Real-estate & property
mgmt, Healthcare admin (non clinico), Construction/field-services, Logistics/freight, Events/hospitality,
Customer success/support, HR/people-ops, Accounting/bookkeeping, Education/tutoring/coaching,
Banking/lending/fintech ops, Procurement/vendor mgmt, Onboarding/implementation (SE post-vendita),
Marketing/agenzie creative, Investor relations/VC — e **qualsiasi altra nicchia di servizio che YC
finanzia** (aggiungile se le trovi):

1. **Enumera le aziende YC REALI** in quel verticale nei batch 2022→2026 (nome, batch, one-liner di cosa
   fanno davvero, a chi vendono). Punta a 8–20 aziende per verticale dove esistono. Fonti:
   ycombinator.com/companies (filtri industry), directory YC, TechCrunch/press, siti aziendali. **Non
   inventare** aziende o batch — se il batch è incerto scrivi "unknown"; se il verticale è sottile su YC
   dillo esplicitamente (confidence).
2. **Valuta l'opportunità avatar** su 4 assi (1–5, 5=meglio): densità meeting + volume follow-up; quanto
   costa l'umano sostituito/aumentato (WTP); bassa frizione regolatoria (5 = niente licenze/HIPAA/EU-AI-Act);
   quanto è LIBERA la lane per l'avatar (5 = il comportamento esatto "avatar-entra-live-ed-esegue" NON è
   ancora occupato).
3. **behaviorOccupied**: il comportamento esatto (entra nel Zoom/Meet/Teams DEL CLIENTE + esegue i
   follow-up) è già fatto da una YC in quel verticale? Sii preciso: chi fa interviste/demo/screening
   sulla PROPRIA superficie video NON occupa questo comportamento. Nomina chi (se qualcuno).
4. **Le 4–6 action specifiche** che l'avatar eseguirebbe in quel verticale.
5. **whyCouldWin** (la tesi: perché l'avatar può vincere QUEL verticale) + **biggestRisk** + un punteggio
   complessivo 1–5.

**Verifica avversariale**: per ogni verticale, prendi il claim competitivo chiave (occupato/whitespace) e
prova a REFUTARLO — i competitor nominati fanno davvero il comportamento esatto o sono solo adiacenti
(notetaker, propria superficie, automazione post-meeting)? Correggi behaviorOccupied dopo la verifica.

**Consegna**:
- Scrivi un catalogo rankato per-verticale in `docs/product/YC-COMPANIES-CATALOG-2022-2026.md` con: le
  aziende reali, i punteggi, why-win, le action, e una sezione fonti (URL).
- Fai anche un **artifact a parte** (pagina leggibile) con tutto: verticali, aziende, perché ogni nicchia
  potrebbe vincere, il #1 e #2 raccomandati, il buyer target, e le 4–6 action da shippare.
- **Segnala esplicitamente** dove la copertura resta incompleta/direzionale (quali verticali o batch non
  hai potuto verificare a fondo).

**Nota per l'orchestrazione**: se puoi, usa un workflow multi-agente (un agente per verticale, poi
verifica avversariale, poi sintesi) — è il modo più esaustivo. Altrimenti la skill deep-research.

**Extra (facoltativo, alla fine)**: apri `docs/product/ROADMAP-2026-07-14.md` e
`docs/product/YC-COMPANIES-CATALOG.md` (il primo giro) e dimmi se, alla luce della nuova ricerca, manca
qualcosa nel roadmap o se la scelta nicchia #1/#2 regge.

---

_Il primo giro (sintesi) è già in `docs/product/YC-COMPANIES-CATALOG.md`: dava #1 Sales B2B consultivo
(whitespace, Caretta refutato), #2 Recruiting via agenzia/ATS. Questa ricerca lo deve rendere molto più
dettagliato e company-level su tutti i batch 2022→2026._
