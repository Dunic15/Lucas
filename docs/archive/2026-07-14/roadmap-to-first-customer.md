# Roadmap: da qui a "un cliente nuovo la può usare" + SFF Studio pronto

_2026-07-12. GOAL: uno sconosciuto si registra su lauravatar.com e usa il loop
completo (login → avatar in meeting → azioni → esecuzione via Cedric → esito in
dashboard) da solo, e tutto è configurato per SFF Studio._

## Legenda: chi può farlo
- 🤖 = Claude/Codex (autonomo, "Ben ok a fixare tutto")
- 👤 = Owner (Duccio) — azione manuale / decisione / pagamento
- 🌐 = Gate esterno (Google/Slack review, non aggirabile da noi)

---

## Cos'è GIÀ fatto e verificato
- Login Google + private-beta gate, dashboard (avatar, dispatch, meeting, summary,
  azioni, Configure solo su Cedric, Add-to-Slack, connettori del brain).
- Contratto Laura↔Cedric e2e: auth per-org, provisioning, provenance.
- **Loop completo dimostrato con email VERA** (capture→proposta→approve→gmail_send→done).
- Bug reali fixati (registry SSM-on-boot, gmail `to`-array, canale per-nome, leave).

## Blocco confermato ORA
- ✅ **RISOLTO (owner, 2026-07-12): credito Anthropic (Cedric) + Recall ricaricati.**
  Le proposte LLM ricche di Cedric e i minuti di meeting reali non sono più bloccati.
  → Da riconfermare con un dry-run/e2e sul path ricco (non solo email singola).

---

## FASE 1 — "demo che gira per un workspace configurato" (giorni: ~1, quasi tutto pronto)
1. 🤖/👤 **Slack Interactivity URL** (staging + prod) → il bottone Approve umano scatta.
   *api.slack.com → app → Interactivity → `…/api/slack/interactions`.* Config app = Ben,
   ma posso guidarlo click-by-click.
2. 🤖 **Prod promotion Cedric**: merge `staging`→`main` (fix gmail + #17 + #18 + test),
   poi `vercel --prod` (👤 Ben lancia, o mi dà VERCEL token prod). Poi 🤖 flip URL Laura
   `SURFACE_*`/`CEDRIC_ORGS_URL` a meet-cedric.com + re-provision.
3. ✅ **Anthropic (Cedric) ricaricato** — fatto 2026-07-12. Path ricco sbloccato.

## FASE 2 — "un cliente ESTERNO si registra da solo" (giorni: 3-7, gate esterni)
4. 🌐 **Google OAuth fuori da Testing**: verificare/pubblicare l'app (o "Internal" nel
   Workspace SFF → tutti gli @sffstudio.com entrano senza allow-list). *Serve admin
   Workspace SFF; la verifica Google può richiedere giorni.*
5. 🌐 **App Slack Cedric distribuibile**: perché il cliente faccia "Add to Slack" sul SUO
   Slack (non il playground). Config pubblica + eventuale review Slack.
6. ✅ **Crediti Recall** ricaricati — fatto 2026-07-12. Minuti meeting reali coperti.
7. 🤖 **Test end-to-end su un workspace fresco**: nuovo utente → Add-to-Slack → connette
   Gmail → dispatch → azione → esecuzione. (Verifica il flusso #17 su tenant vergine.)

## FASE 3 — "pronto per SFF Studio" (parallelo)
8. 🤖 **Dominio pulito** `app.lauravatar.com` (App Runner custom domain + Cloudflare DNS).
9. 👤+🤖 **Rotazione chiavi** esposte (Cerebras/Anthropic/ElevenLabs/Recall) → 🤖 aggiorno SSM.
10. 🤖 **Avatar conversazione multi-persona** (vedi sotto — lavoro prodotto continuo).

## FASE 4 — scala oltre i primi utenti (dopo)
11. 🤖 **Multi-tenancy vera** (Postgres/RLS al posto di SQLite+litestream).
12. 🤖 Osservabilità, rate-limit per-org, billing reale.

---

## "Migliorare le conversazioni degli avatar nei meeting + più persone"
Track prodotto separato (core: "latency is the product"). Aree:
- **Turn-taking multi-persona**: deference quando è nominata un'altra persona, non
  parlare sopra, riprendere il turno correttamente (già iniziato: #82/#115/#120).
- **Roster/diarization**: chi ha detto cosa, indirizzare per nome, gestire 3+ voci.
- **Naturalezza**: backchannel, barge-in, grazia d'apertura, emozione (#113).
- → Lo lavoro io (o Codex) come stream dedicato; vedi issue tracker.

## Come dare accesso "a tutto SFF Studio"
Due significati:
- **Login**: rendere l'app Google **"Internal"** nel progetto del **Workspace SFF**
  (non l'attuale progetto personale `868562221752`). Serve un **admin del Google
  Workspace SFF**. Risultato: ogni `@sffstudio.com` entra senza allow-list.
- **Repo/infra**: già hai admin su `SFF-Studio/Cedric`; per farmi operare serve solo
  che io abbia AWS creds + `gh` auth (li ho).
