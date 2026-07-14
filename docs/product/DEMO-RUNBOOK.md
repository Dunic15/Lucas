<!-- The exact, verified, gap-avoiding demo script. Every surface below was dry-run-verified live in prod 2026-07-13 after the 7 hardening PRs. -->

# Laura — Demo Runbook (verificato 2026-07-13)

Copione per una demo **che vende**, testato dal vivo. Inquadra su **2-4 persone** (dove il multi-persona brilla). Backend: `https://dhfgfe6yw6.eu-central-1.awsapprunner.com`.

## Pre-demo (30 sec)
- `curl .../health` → `active_sessions: 0` (nessuna sessione orfana che brucia il meter).
- Multi-persona: i default sono conservativi e già live. Se in una prova risultasse loquace: env `HAND_RAISE_INTERJECT_WHEN_CONFIDENT=false`. Se timida: abbassa `HAND_RAISE_INTERJECT_MIN_CONFIDENCE`.
- Tieni pronta: una knowledge SOP (già seedata per laura/sff), un Google Meet di cui sei **host**.

## Beat 0 — "È viva, e non ha bisogno di chiavi" (2 min, pubblico, zero rischio)
1. Apri **`/talk`** → faccia 3D + voce reale. *"Questa è Laura. Nessuna chiave, gira ora."*
2. **`/demo/ask`** (o la pagina): chiedi una domanda di processo → risposta **citata** dal doc. Poi una di world-knowledge ("chi ha vinto i mondiali 2022") → **rifiuta onestamente** (`sufficient:false`). *"Risponde solo dal VOSTRO sapere, e ammette quando non sa — non un chatbot che inventa."*

## Beat 1 — Il momento wow multi-persona (LIVE, il cuore)
1. `/join` → incolla l'URL Meet → Avatar **Laura** → token **vuoto** → **Send Laura**. **Annota il `bot_id`.**
2. Ammettila dalla waiting room.
3. **La mossa garantita:** un umano guarda in camera e la chiama **per nome**: *"Laura, abbiamo confermato il DPA su questo account?"* → il path by-name salta hand-raise/deference → risponde **diretta, veloce, grounded, citata**. È la reazione che compri: *"è intervenuta ed era giusta."*
4. (Nuovo, spedito oggi) Su un'affermazione grounded di un umano con floor aperto, ora **interviene con UNA riga** invece della mano alzata silenziosa — ma condivide il budget (max 4/call), non domina.

## Beat 2 — La cattura (il payoff)
In chiusura (di' *"chiudiamo?"* o lascia una pausa) → Laura segnala **uno step mancante** + **readiness score** a schermo. *"Un notetaker ve l'avrebbe detto la settimana dopo. Lei l'ha catturato nella stanza."*

## Beat 3 — "È stato fatto davvero" (la dashboard)
`…/login` (col TUO Google) → dashboard: **outcome, non note** — readiness, azioni con owner, chip "delivered", ROI (ore follow-up automatizzate). *Poi:* in **`#test-laura`** (SFF Studio) è arrivato il **recap + la card di approvazione** di Cedric — il loop chiuso.

## Chiusura vendita
Il pitch dell'[one-pager](../gtm/PRICING-PITCH-ONEPAGER.md): *"Un riassunto ti dice cosa ti sei perso. Laura ti impedisce di perderlo."* Ancora sul **go-live salvato**, non sul "notetaker migliore". Tier Pro $59 / Business $149.

---

## ⚠️ Cosa NON mostrare / come aggirare i gap (finché non chiusi)
- **Click su "Approve" nella card Slack:** il bottone diventa cliccabile solo con l'Interactivity URL di Ben. Nel frattempo: **non cliccarlo dal vivo** — narra *"e approvando, Cedric esegue"* e mostra l'esecuzione reale via harness (`POST /api/laura/test/approve`) prima della demo, oppure usa un'azione **schedule_task** (non serve Gmail).
- **Email reale:** serve il connettore Gmail (Ben) → usa azioni che non inviano email (schedule/recap) nella demo dal vivo.
- **Registrazione di uno sconosciuto:** l'OAuth pubblico non è attivo → il prospect vive i **surface pubblici** (`/talk`, `/demo`), tu guidi la dashboard dal tuo login. Non serve per una demo guidata.
- **Numeri > 4 persone:** inquadra su 2-4 (i comportamenti sono nitidi lì).

## 🔴 Meter safety (SEMPRE)
A fine demo: `curl -X POST …/sessions/<bot_id>/end` → poi `…/health` deve dare `active_sessions:0`. Ogni sessione live brucia il meter Recall al minuto.

## Blemish da chiudere prima di condividere il link del sito
Il `<title>` di `lauravatar.com` è ancora "My Framer Site — Made with Framer" (Framer → **Site Settings** → title/description/og-image, 2 min). Undercutta ogni anteprima social/Google.
