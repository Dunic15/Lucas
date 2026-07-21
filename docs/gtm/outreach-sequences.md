# Outreach Sequences: First Interest-Test Batch

**Status: copy only, not sent.** Draft sequences for the ~32-lead interest test
across Niche 1 (SaaS, English) and Niche 4 (HR/recruiting Italy, Italian).
Paste into whatever tool sends them (see cadence below); review every
`{{pain}}` value before sending; do not send a lead with a blank or
low-confidence pain field without swapping in the fallback line noted below.

Source lists: `docs/gtm/niche1-saas-leads-with-emails.xlsx` (17 leads),
`docs/gtm/niche4-hr-recruiting-italy-leads-with-emails.xlsx` (15 leads).
Company/pain hypotheses per `docs/gtm/target-accounts-clay-brief.md` and
`docs/gtm/niche4-hr-recruiting-italy-prospects.md`.

## Merge fields (only what we have: nothing invented)

| Tag | Source column | Notes |
|---|---|---|
| `{{company}}` | company | |
| `{{first_name}}` | contact first name | |
| `{{role}}` | role/title | used sparingly; only where it reads naturally |
| `{{city}}` | city | used once per sequence at most, light touch |
| `{{pain}}` | pain | short pain-hypothesis phrase per lead; **if blank, use the fallback sentence below instead of sending a broken merge** |

Not used as merge tags in copy: `country`, `website`, `LinkedIn`: kept in
the sheet for your own targeting/QA, not referenced in the emails (no
invented personalization beyond the five fields above).

**Fallback if `{{pain}}` is empty or low-confidence for a lead:**
- Niche 1 (English): *"a required step, a security review, an access
  sign-off, a DPA, gets missed on the kickoff call"*
- Niche 4 (Italian): *"una decisione presa a voce durante il debrief non
  viene messa per iscritto e si perde per strada"*

## A/B split + cadence

- Split each niche roughly in half between **Angle A** (readiness /
  early-warning) and **Angle B** (decisions & owners captured). Niche 1:
  ~8/9. Niche 4: ~7/8. At this sample size treat results as **directional,
  not statistically significant**: see "What to measure."
- **Day 0**: Email 1 (A or B, per the split).
- **Day 3–4**: Follow-up 1 (bump), same thread.
- **Day 8–9**: Follow-up 2 (break-up), same thread. Stop after this -
  no further touches without a reply.
- Single CTA throughout, low-friction and link-free: reply and get a
  90-second recording of Laura on a call like theirs. No calendar link, no
  attachment, no "book a call" ask unless they ask for one.
- `[Your name]` / `[Il tuo nome]` and the compliance footer (see below) are
  placeholders; fill in before sending, not fabricated here.

---

## Niche 1: SaaS / Customer Success & Onboarding (English)

Pain hypothesis: onboarding/implementation kickoff calls where a missed
step (security review, access provisioning, DPA) stalls go-live.

### Email 1: Variant A: readiness / early-warning score

**Subject line variants**
1. quick one on {{company}}'s onboarding calls
2. before a kickoff call goes sideways
3. an early-warning score for go-live risk

**Body**
```
Hi {{first_name}},

Most kickoff calls at {{company}} probably go fine; until the one where
{{pain}}, and go-live slips a week or two.

I'm building Laura; an AI that joins the actual kickoff call (Zoom/Meet/
Teams), tracks it silently against your own onboarding process doc, and
carries a live readiness score through the call. If a required step is
about to get missed as the call wraps, she flags it once; before
everyone hangs up and finds out next week.

She's not a notetaker. She's grounded in your process, not just
recording the call.

Worth a 90-second recording of her running on a kickoff-style call? No
deck; just reply "send it" and I will.

[Your name]
```

### Email 1: Variant B: decisions & owners captured

**Subject line variants**
1. what got decided on your last kickoff call?
2. nothing dropped between onboarding calls
3. {{first_name}}: who owns what after a kickoff call?

**Body**
```
Hi {{first_name}},

After a kickoff call, someone usually has to reconstruct what got
decided and who owns what; especially when {{pain}}.

I'm building Laura; an AI that joins the actual call (Zoom/Meet/Teams),
listens, and can answer process questions live, grounded in your own
onboarding docs. When the call ends, she hands you a written artifact:
decisions made, action items with owners, and what's still open. No one
has to rebuild it from memory or a recording.

Not a transcript; an output your team can act on the same day.

Worth a 90-second recording of it running on a real kickoff call? Reply
"send it" and I'll send one over; no deck attached.

[Your name]
```

### Follow-up 1: bump (Day 3–4)

**Subject line variants**
1. still stalls on go-live calls at {{company}}?
2. bumping this
3. quick nudge, {{first_name}}

**Body**
```
Hi {{first_name}}; following up in case this got buried.

Still happy to send the 90-second recording if useful. Short version:
Laura joins your onboarding kickoff calls, catches the step that
would've slipped through, and hands you decisions + owners the moment
the call ends.

Worth a look?

[Your name]
```

### Follow-up 2: break-up (Day 8–9)

**Subject line variants**
1. closing the loop
2. should I stop reaching out?
3. last note from me, {{first_name}}

**Body**
```
Hi {{first_name}}: I'll take the silence as "not now," and I won't
keep filling your inbox.

If a missed step on a kickoff call ever costs you a week of go-live, the
offer stands; reply anytime and I'll send the 90-second recording.

Good luck with the {{company}} launches.

[Your name]
```

---

## Niche 4: HR / Recruiting Italy (Italian)

Pain hypothesis: debrief candidato/cliente e call di coordinamento dove
le decisioni (avanti/no, prossimo step, owner) non vengono fissate,
allungando il time-to-hire.

*Nota sul registro:* copy in "tu", coerente con il tono diretto usato
oggi dalle recruiting/HR boutique italiane più moderne (es. Hunters
Group, EgoValeo). Se il destinatario è una realtà più formale/storica,
passare al "Lei" è una sostituzione meccanica, non serve riscrivere.

### Email 1: Variante A: punteggio di readiness / allerta precoce

**Varianti oggetto**
1. una cosa veloce sui vostri debrief
2. prima che un debrief vada storto
3. {{first_name}}, un allarme precoce su chi avanza

**Corpo**
```
Ciao {{first_name}},

La maggior parte dei debrief dopo un colloquio va bene; finché non
capita quello in cui {{pain}}, e il time-to-hire si allunga di una
settimana.

Sto costruendo Laura, un'AI che partecipa davvero alla call (Zoom/Meet/
Teams) insieme al team di {{company}}, ascolta e tiene traccia in
silenzio del processo; chi avanza, chi viene scartato, chi deve fare il
prossimo passo. Se un passaggio critico rischia di restare senza
decisione a fine call, lo segnala una sola volta, prima che tutti si
disconnettano.

Non è una trascrizione. È collegata al vostro processo, non si limita a
registrare.

Vale la pena un video di 90 secondi che la mostra in azione su un
debrief tipo? Niente da scaricare; rispondi "mandalo" e te lo giro.

[Il tuo nome]
```

### Email 1: Variante B: decisioni e owner catturati

**Varianti oggetto**
1. cosa è stato deciso nell'ultimo debrief?
2. niente si perde tra una call e l'altra
3. {{first_name}}, chi si occupa del prossimo step?

**Corpo**
```
Ciao {{first_name}},

Dopo un debrief candidato-cliente, di solito qualcuno deve ricostruire
a memoria cosa è stato deciso e chi deve fare cosa; soprattutto quando
{{pain}}.

Sto costruendo Laura, un'AI che partecipa alla call (Zoom/Meet/Teams),
ascolta, e può rispondere a domande di processo in tempo reale,
basandosi sui vostri documenti interni. Quando la call finisce,
consegna un riepilogo scritto: decisioni prese, action item con owner,
cosa resta aperto; nessuno deve ricostruirlo da un audio o a memoria.

Non è un verbale. È un output su cui il team può agire subito.

Vale la pena un video di 90 secondi che la mostra su un debrief vero?
Rispondi "mandalo", niente da scaricare.

[Il tuo nome]
```

### Follow-up 1: rilancio (Day 3–4)

**Varianti oggetto**
1. rilancio questa mail
2. ancora capita a {{company}}?
3. un promemoria veloce, {{first_name}}

**Corpo**
```
Ciao {{first_name}}; un rilancio, nel caso si fosse persa tra le altre
email.

Il video di 90 secondi resta disponibile se utile. In due righe: Laura
partecipa ai vostri debrief, segnala il passaggio che sarebbe rimasto
senza decisione, e a fine call consegna decisioni + owner.

Ti va di dare un'occhiata?

[Il tuo nome]
```

### Follow-up 2: chiusura (Day 8–9)

**Varianti oggetto**
1. chiudo qui
2. devo smettere di scrivere?
3. ultimo messaggio, {{first_name}}

**Corpo**
```
Ciao {{first_name}}; il silenzio lo prendo come un "non ora", e non
continuerò a scrivere.

Se un giorno un passaggio deciso a voce e poi perso vi costerà una
settimana di time-to-hire, l'offerta resta valida; rispondi quando
vuoi e ti mando il video di 90 secondi.

In bocca al lupo con le ricerche in corso per {{company}}.

[Il tuo nome]
```

---

## Deliverability & compliance

- **Separate sending domain.** Send from a dedicated domain/subdomain -
  never the main `lauravatar.com`. A cold-outreach domain that gets
  flagged degrades or burns the primary domain's reputation.
- **Warm-up.** Run 2–3 weeks of warm-up traffic on the new domain/mailbox
  before this batch goes out. Configure SPF, DKIM, and DMARC on the
  sending subdomain first; no warm-up or real sends without them.
- **Daily volume caps.** New domain/mailbox: start ~10–20 sends/day,
  ramp toward 30–50/day max. At 32 leads total, there's no need to rush -
  spread the batch over several days rather than sending it all at once;
  low volume itself is a deliverability asset here.
- **Plain text over HTML.** No tracking pixels, no branded HTML template,
  no link-shorteners. The CTA is reply-based ("reply and I'll send it") -
  no link at all in the first two touches, which also helps spam-filter
  scoring.
- **Spintax / variation.** Rotate the subject-line variants above (and
  vary the opening line where natural) across the batch so recipients on
  the same mail server don't see byte-identical messages; reduces
  pattern-matching spam flags, independent of the A/B split test.
- **GDPR: B2B legitimate interest.** This is role-relevant B2B outreach
  to a business email address about a work-relevant tool; legitimate
  interest is the usual basis, but **Italy's Garante Privacy is stricter
  on cold B2B email than most EU states.** Treat the Italian batch with
  extra care and get a real legal/compliance read before scaling past
  this test batch; this is not legal advice.
- **One-click opt-out.** Every email must let a recipient stop the
  sequence trivially: "reply STOP and I'll remove you" is enough for a
  batch this size (no link infrastructure needed). Honor it immediately;
  never re-add an opted-out contact to a future batch.
- **Sender identity footer.** Add a plain-text footer with the real
  sending entity name and a contact address before sending; required
  under CAN-SPAM and best practice under GDPR. Not written into the
  copy above; add it as a fixed footer in the sending tool.

## What to measure

- **Reply rate** (any reply, % of sent): top-of-funnel signal that the
  subject line + opener got through and got read.
- **Positive-reply rate** (% of sent that engage with interest, asks a
  question, wants the recording, separated from "not interested" and
  auto-replies/OOO); the real signal at this stage.
- **Meeting/demo-take rate** (% who actually take the CTA: watch the
  90-second recording or ask for the 10-minute look); the bottom-line
  number for this test.
- **Open rate is unreliable: do not use it to decide anything.** Apple
  Mail Privacy Protection and Gmail image proxies inflate opens for
  everyone; plain-text emails with no pixel may show 0% opens even when
  read. At most, treat a near-zero open rate across the *whole* batch as
  a rough deliverability sanity check, not a copy signal.
- **Sample-size honesty.** With ~8–9 leads per angle per niche, this is a
  directional read, not a statistically significant test. Don't over-index
  on one or two replies either way.
- **Kill / scale read:**
  - **Kill / rewrite:** zero replies across both angles and both niches
    after all 3 touches → treat as a messaging or targeting miss; revisit
    copy or the ICP hypothesis before sending another batch. Don't scale
    volume on unproven copy.
  - **Lean scale:** one angle clearly outpulls the other (e.g., 3+ replies
    vs. 0–1 on a similar-sized split) → adopt the winning angle as the
    default opener for the next batch; still re-test at larger volume
    before locking it in.
  - **Scale:** any reply that takes the CTA (watches the recording, asks
    for the 10-minute look) is a green light to prioritize warm,
    human-in-the-loop follow-up with that lead over building more
    automated volume; a single real conversation beats another 50 sends
    at this stage.
