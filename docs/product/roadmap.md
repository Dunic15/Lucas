# Roadmap generale: Laura (agg. 2026-07-14)

Fonte: demo del 14/07 con Christian Mischler + Jacopo Trinca (transcript analizzato) + direttive owner.
Doc collegati: [NATIVE-INTEGRATIONS-PLAN.md](NATIVE-INTEGRATIONS-PLAN.md) ·
[YC-NICHE-MINING.md](YC-NICHE-MINING.md) · [YC-COMPANIES-CATALOG.md](YC-COMPANIES-CATALOG.md) (in corso) ·
[PHOTOREAL-DITTO-PLAN.md](PHOTOREAL-DITTO-PLAN.md).

## Resume del meeting (cosa hanno detto)
- **Christian: onboarding**: due registrazioni (Cedric + LLM key) = troppa friction. "Cedric doesn't
  even exist in this; it's just an LLM." Connettori **nativi in Laura**; l'utente porta solo la key.
  Rischio (irrisolto): sicurezza delle API key incollate.
- **Christian: in meeting**: turn-taking come **opzione admin** + **DM di preview al host**.
- **Christian: strategia**: UN ambiente demo perfetto; una azione perfetta alla volta; nicchia dove
  **gli umani costano tanto**; mission per-meeting (analyst/paralegal).
- **Jacopo**: Cedric = "add-on, pay tier, optional"; niente compliance; **scrape batch YC** per la nicchia.
- **Duccio**: task deterministici nel knowledge (modello Lemonslice).
- **Decisioni**: no nicchia compliance; UN use case validato.

## ✅ FATTO oggi (14/07): tutto in prod, flag/off-path-safe
- **Fix demo-blocker**: connettori SFF (raw-blob + anti-clobber Cedric #33–#39), no-refresh (#189),
  connect riflesso (#28/#190), descrizioni avatar (#188), disconnect 502 (IAM), dedup approval (#36),
  ask-back tolto (roster-resolve #37), crash 500 self-serve u_hash (#196), Upcoming Meetings calendario (#195).
- **Native executor (fondamenta + loop)**: #199 (OAuth write + token store cifrato + google_client) +
  #200 (azioni tipizzate a finalize + Approve→execute + receipt "Done ↗" + toggle native/cedric).
  **`NATIVE_EXECUTOR` OFF di default** → comportamento identico a oggi finché non si accende.
- **Avatar più smart** (#201): mission per-meeting + task deterministici (entrambi off-path-safe).
- **Testabilità live** (#203): campo **Mission (optional)** nel form dispatch (basta la mission, niente
  brief obbligatorio) + **bottone "Approve & run" per-riga** in dashboard (con flag OFF → "captured ·
  execution off", dry-run del giro produce→approva). **Mission testabile subito; executor Livello A subito.**

## NOW (questa settimana)
| # | Item | Owner | Note |
|---|------|-------|------|
| 1 | **Accendere il native executor (Livello B)**: bottone Approve per-riga ✅ (#203); resta: `GOOGLE_TOKEN_ENC_KEY` + flip `NATIVE_EXECUTOR=true` (una env-deploy) + consenso Google write (scope `calendar.events`+`gmail.send`) | Claude | env-deploy → coordinare con la sessione Gemini; + security-gate crypto (sotto) |
| 2 | **Photoreal Ditto, Fase 1 (il vero fix)**, ripristina immagine v3 sul pod, verifica `/stream`, e2e in Meet → photoreal-che-parla 720p. Vedi PHOTOREAL-DITTO-PLAN.md | Claude (infra) | ½ giornata; dire "Fase 1" per partire |
| 3 | **Connettori a due toggle indipendenti**, nella view Connections: **«Aggiungi Slack»** (add-on via Cedric) + **«Aggiungi Google»** (nativo, Calendar+Gmail write, alimenta l'executor, zero Cedric). Attivabili singolarmente o insieme | Claude | PR draft in corso; deploy dopo Gemini |
| 4 | **Etiquette "scusarsi entrando"** (estende first-call activation) | Claude | live-path: review attenta |
| 5 | **Security crypto**: prima di accendere l'executor: KMS/Fernet al posto della crypto stdlib fatta a mano | Claude | GATE per #1 |
| 6 | **Fix calendar-connect Cedric** (stale-read serverless) | **Ben** | atomica #39 in main; resta il read runtime |

## NEXT (1–2 settimane)
| # | Item | Owner |
|---|------|-------|
| 7 | **Photoreal Ditto. Fase 2**: auto-wake zero-touch + tuning "ottimo 720p" + deploy #193 | Claude (infra) |
| 8 | **OAuth per-org** (ogni utente il suo Google) + fix attribuzione auto-join (oggi → org di default) | Claude |
| 9 | **Ricevute con provenance** (ogni "Done" col link vero evento/mail) | Claude |
| 10 | **Dashboard semplificata** (via cost line, model badge, colonne morte; resta lista+coda approvazioni+ricevuta) | Claude |
| 11 | **Intervention-mode admin** + **host-preview DM** | Claude |
| 12 | **Demo account "setting perfetto"** (investor/YC) | Duccio + Claude |
| 13 | **API-key self-serve** (bring-your-own key in un vault) | **Ben** + Claude |

## LATER
| # | Item | Owner |
|---|------|-------|
| 14 | **Scelta nicchia** ← YC-NICHE-MINING.md (Recruiting #1 data-backed) + YC-COMPANIES-CATALOG.md (censimento esaustivo, in corso) → decisione owner/Christian/Jacopo | Duccio + Claude |
| 15 | **1 avatar tailored** sulla nicchia scelta (le action mappate diventano capability) | Claude |
| 16 | **Photoreal Ditto. Fase 3**: web call ultra-HD/4K off-meeting (unico posto oltre i 720p) | Claude (infra) |
| 17 | Email vere per-avatar (multi-tenancy) | Claude |
| 18 | **GPT Live experiment**: A/B OpenAI Realtime (`gpt-realtime`) vs Gemini Live sul path voce+turn-taking; metrica: latenza, qualità turn-taking, €/min, se batte lo split Cerebras-brain + ElevenLabs-voce | Claude (dopo Gemini) |

> **One-login: DEPRIORITIZZATO** (owner: di fatto già così, login Cedric una volta sola).

## Analisi Y Combinator (per scegliere la nicchia)
- **YC-NICHE-MINING.md** (fatto): shortlist rankata → **Recruiting/Staffing #1** (23/25), concept "Talia";
  runner-up Sales/Consulting; caveat: lane affollata + EU AI Act (doc, non licenza).
- **YC-COMPANIES-CATALOG.md** (in corso, deep-research): censimento esaustivo per-vertical con le aziende
  YC reali + fit-avatar + action specifiche per nicchia; sostituisce la memo generale.
- Fattore pragmatico: **VC/SFF e Founder-EA hanno già avatar+knowledge nel repo** → via più veloce.

## Brain & voice provider (LLM track)
- **Oggi in prod (live)**: `BRAIN_PROVIDER=groq` + `GROQ_BASE=api.cerebras.ai` → **Cerebras `gemma-4-31b`** (fast/live);
  post-meeting = **Anthropic Sonnet 5** (`BRAIN_PROVIDER_POST=anthropic`).
- **In corso (sessione Duccio)**: switch live a **Gemini** (Vertex/Flash). Codice provider pronto (#202,
  gemini come OpenAI-compat). Env: `BRAIN_PROVIDER=gemini`, `BRAIN_MODEL_FAST=gemini-2.5-flash`,
  `GEMINI_API_KEY` in SSM. **Duccio deploya nella sua sessione** (env-deploy serializza. Claude sta fuori).
- **Later: GPT Live experiment (#18)**: A/B **OpenAI Realtime (`gpt-realtime`)** vs Gemini Live sul path
  voce+turn-taking (speech-to-speech). Metrica: latenza, qualità turn-taking, €/min, se batte lo split
  attuale Cerebras-brain + ElevenLabs-voce. Parte dopo che Gemini è a regime.

## Integrazioni native (in corso, multi-sessione)
- **Upcoming Meeting ↔ Google Calendar dell'utente** (altra sessione, appena implementato): la coda
  Upcoming si popola dal Google Calendar personale dell'utente (OAuth per-org, NEXT #8) invece che
  dall'attribuzione all'org di default.
- **Native executor (Approve → Calendar/Gmail)**: fondamenta+loop+bottone in prod (#199/#200/#203),
  flag OFF → accensione Livello B coordinata con Gemini (vedi NOW #1).

## Cose per BEN
1. **Calendar-connect Cedric** (stale-read runtime Vercel/Neon): fix atomico #39 in main.
2. **API-key self-serve** in sicurezza (vault).
3. **One login**: deprioritizzato ma da confermare la meccanica se lo riprendiamo.
4. Review PR Cedric #33–#40.

## Guardrail
Contratto live-meeting intatto; demo key-free intatta (`NATIVE_EXECUTOR` off default); org Cedric
invariate; transcript = PII (in memoria, mai nei log); crypto token → KMS/Fernet prima del go-live executor.
