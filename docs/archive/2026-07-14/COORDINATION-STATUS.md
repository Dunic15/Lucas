# Laura: Coordination Status

_Maintained by the **integrator session** (the one Claude session that converges
parallel work onto `origin/main`). Last updated: **2026-07-14**._

## 📡 SEGNALE sessione Gemini (2026-07-14 sera): FATTO, leggere prima di deployare

**Cosa è andato in prod stasera (2 merge + 3 deploy App Runner, tutti SUCCEEDED, guard rispettate):**
1. **#204 MERGED + deployed** (`c8b4985`): `BRAIN_PROVIDER=vertex` first-class (Gemini via
   Vertex = gira sui crediti GCP; l'AI-Studio key NON è coperta dal free trial) + 2 spike voce.
2. **#209 MERGED + deployed** (`c03f278`): **Gemini ears**: audio Recall (`audio_mixed_raw`,
   WS realtime endpoint cap-bound → `/realtime/recall-audio`) → Gemini Live STT + end-of-turn.
   Suite 929 verdi. ⚠️ tocca `main.py`/`recall_client.py`/`llm.py`/`config.py` → **#202, #177,
   #166, #153 e i codex P0 vanno rebasati su origin/main**.
3. **Env-deploy fatto**: aggiunti `VERTEX_PROJECT`, `VERTEX_LOCATION=us-central1`,
   `GOOGLE_VERTEX_SA_JSON` (SSM `/laura/prod/GOOGLE_VERTEX_SA_JSON`, SecureString, SA
   `laura-vertex@eng-cache-501113-v2` con `roles/aiplatform.user`, validata HTTP 200) e
   **`GEMINI_EARS_MODE=shadow`** (metriche-only, zero impatto live: `GET /gemini-ears/status`).

**⚠️ Correzione alla nota "quando flippi BRAIN_PROVIDER=gemini": quel flip NON è in programma.**
Il live resta **Cerebras** deliberatamente (primo token ~0,17s; Vertex ~0,4-0,6s; la latenza è
il prodotto). `vertex` resta pronto come opzione env. Nota anche: su main il provider è `vertex`
(#204); `gemini` (#202, OpenAI-compat/AI-Studio) è un provider DIVERSO ancora open.

**👉 Il momento giusto per bundlare il Livello B executor (enc-key + NATIVE_EXECUTOR):**
il **prossimo env-deploy della sessione Gemini** = flip `GEMINI_EARS_MODE=shadow→on`, previsto
subito dopo la shadow-validation (1 call di prova dell'owner, criterio: turns>0, connected,
speaker_matched sano). Gate per il bundle: **#205 mergiato prima**. Se mi lasciate qui (o in chat)
il nome del param SSM dell'enc-key, includo `NATIVE_EXECUTOR=true` + `GOOGLE_TOKEN_ENC_KEY` nello
stesso `update-service`: una sola riconnessione Google per gli utenti, un solo restart.

## ⏩ COORDINATION UPDATE (2026-07-14): 2 persone, split per LAYER strategico

Ora si lavora **in due**. Split del lavoro per **layer** (vedi
[`STARTUP-DIRECTION.md`](STARTUP-DIRECTION.md)) → un owner per layer, deploy serializzati.

**Stato reale (verificato):** `origin/main` = **#203** (native executor #199/#200/#203 landed,
**flag OFF**). Gemini in volo: **PR #204** (Vertex) + **#202** (OpenAI-compat) OPEN. Crypto gate
**PR #205 DRAFT + MERGEABLE** (Fernet + fail-closed; 910 test verdi). Grosso backlog draft
(#192/#193 photoreal, #194 leave, #177 org_id, #166 billing, #153 self-serve, #160–168 codex P0).

**Ownership per layer (chi possiede cosa):**

| Layer / filone | Owner | File caldi | Deploy? |
|---|---|---|---|
| **L2 · Nicchia + esecuzione (moat)** | Owner (Duccio) + Claude | executor/ledger/store, template avatar | flip executor = env-deploy (owner) |
| **L3 · Connettori / one-click (distrib.)** | **Persona B (codice)** | `dashboard.py` (CALDO), `config.py`, `cedric/` | ❌ solo branch/draft, no App Runner |
| **L1 · Realismo + multi-persona** | ripartibile (live-path) | `main.py`, `decision.py`, `meeting_state.py` | review attenta, no deploy in parallelo al flip |
| **Gemini (brain) + deploy** | Owner / sessione Gemini | `brain.py`, `llm.py`, env | ✅ **possiede App Runner** |
| Slack URL / calendar-connect | Ben | app Slack Cedric / Vercel | - gate esterni |

**⚠️ Hot-file `dashboard.py`:** lo toccano già #204/#177/#166/#153 + la Connections (L3). Chi fa
Connections lo **possiede** per quella finestra → **branch corto, mergiare presto**, rebase su
`origin/main` appena uno di quei draft merge. `config.py` = modifica piccola, coordinare in chat.
I PR Gemini toccano `brain.py`/`llm.py` → **non** collidono con Connections (ok in parallelo).

**Ordine di merge macro:** `#204 Gemini` → `#205 crypto` → **flip executor (env-deploy)** →
`Connections F1–3` → `scelta nicchia` → `1 avatar tailored + write profondo` → `OAuth per-org #7`
→ `connettore CRM/ATS #16`.

**Regola d'oro deploy (invariata):** UNA sola persona possiede i deploy App Runner (serializzano).
Pre-merge/deploy come step a sé: `GET /health` → `active_sessions==0` **e** `aws apprunner
list-operations` non `IN_PROGRESS` **e** `gh pr list`. Draft PR di default. `origin/main` è la verità.

**Doc di riferimento (nuovi, 2026-07-14):** [`STARTUP-DIRECTION.md`](STARTUP-DIRECTION.md) (i 3 layer),
[`CONNECTIONS-PLAN.md`](CONNECTIONS-PLAN.md) (L3), [`PRODUCT-ROADMAP.md`](PRODUCT-ROADMAP.md),
[`YC-COMPARISON.md`](YC-COMPARISON.md) + [`YC-TRENDS-2022-2026.md`](YC-TRENDS-2022-2026.md) (il moat/L2).

---

## ⏩ END-STATE UPDATE (2026-07-13): founder/hardening session

`origin/main` GREEN, **558 key-free tests**, and now **CI-gated** (#134 merged: every PR runs the key-free suite). Deploy topology unchanged (App Runner auto-deploys main).

**Shipped this session (founder/production-hardening; all live + verified in prod):**
- ✅ **#132** security: `/live/token` (Anam-billable) + `/gmail/status` (joinable Meet URLs) gated.
- ✅ **#140** multi-tenancy `org_id` spine (SQLite-first): every row org-scoped, isolation-tested, review-blocker (meter-safety) fixed.
- ✅ **#141** dashboard shows OUTCOME (ROI + delivered) + real `readiness_score`.
- ✅ **#142** multi-person turn-taking: confident single-interjection (shared budget) + faster deference + closing fallback. **2 adversarial-review blockers fixed** (over-fire + called-turn latency). Env-tunable.
- ✅ **#143** rate-limiting (public expensive endpoints) + safe security headers (frame-blocking omitted so the avatar page stays Recall-embeddable).
- ✅ **#134** CI gate (this converge).
- 📄 Docs: `DEMO-READY-ROADMAP.md`, `MULTI-TENANCY-IMPLEMENTATION.md`, `PRICING-PITCH-ONEPAGER.md`, `DEMO-RUNBOOK.md`, `FOUNDER-SESSION-HANDOFF-2026-07-13.md`.

**COORDINATION, file ownership (no-collision zones):**
- **This (founder/backend) session owns:** backend logic, `main.py` (webhook/turn-taking), `decision.py`, `ledger.py`, `store.py`, `config.py`, `security.py`, `org_api.py`, security/tenancy/rate-limit.
- **Avatar session owns:** the FACE + dashboard-portraits: `frontend/talk.html`, `/photoreal`, DITTO/emotion, `frontend/dashboard.html`. **Their open PRs, theirs to land:** #130 (emo-intensity, draft), #133 (dashboard avatar portraits, draft; touches `dashboard.html`+`test_dashboard.py`), #139 (action-bridge-test, draft). I did NOT touch those files.
- **Rule:** dashboard **render/value keys** (dashboard.py) = mine (shipped #141); dashboard **avatar portraits** (dashboard.html) = avatar session (#133). Merge #133 after its session marks ready + rebases on current main.

**Human-gated (not code; see `FOUNDER-SESSION-HANDOFF-2026-07-13.md`):** 👤 Supabase · Google OAuth public · DNS · pricing lock · live multi-person test · Litestream reset. 🧑‍💻 Ben: Cedric per-workspace bearer · Slack Interactivity URL · Gmail-send · `laura_org_links`.

---

## ⏩ END-STATE UPDATE (2026-07-12, evening)

**`origin/main` is at #127 and GREEN. 476 passed, 0 failed. App is complete,
healthy, and deployed (App Runner RUNNING).** Convergence outcome:

- ✅ **#123** (`codex/close-turn-findings`, trailing-emoji end-of-turn). **MERGED** by
  this integrator (chosen over duplicate **#126**, which was closed). Deployed.
- ✅ A **parallel integrator** landed **#124** (codex-fixes-train) and **#127**
  (authenticated artifact redelivery); main absorbed them cleanly.
- ✅ **#125** (`claude/hand-raise-etiquette`): **MERGED + deployed (green, 486 tests, App
  Runner SUCCEEDED/RUNNING).** Shipped with the etiquette session's documented default
  `first_call_required=True` (she's a silent guest until named once; intentional design).
  ⚠️ **Demo escape hatch:** if a 1:1/unnamed meeting should have her proactive without being
  named (README's "no name needed for grounded questions"), set env `FIRST_CALL_REQUIRED=false`
  - one App Runner env change, no code. Optional future tuning: a time-boxed cap so the
  wrap-up "missing critical step" intervention isn't gated forever when unnamed.
- ❌ **`codex/finish-product-loop`** (5 commits): **FULLY SUPERSEDED by main; do NOT
  merge (it conflicts + regresses).** Its SSM registry work is overtaken by main's
  better per-org SecureString version (`_read_dedicated_locked`); Recall-507
  humanization is already on main; leave-after-capture is already fixed on main
  (same 2026-07-10 repro, more completely). **Recommend: close the branch.**
- 🔵 **#122** (docs coord handoff): stale, superseded by THIS file; low value, not
  worth a standalone deploy. Close or leave.

---


> Read this first if you are one of the parallel Claude/Codex sessions. It tells
> you what is already merged, what is still yours to land, and in what order -
> so we don't collide on hot files or race a deploy.

---

## 1. Baseline: `origin/main` is HEALTHY ✅

Verified in a clean worktree checked out at `origin/main`:

```
464 passed, 3 xfailed, 0 failed   (backend suite, key-free stub+hash)
```

**The app is not broken.** Most of the parallel work is already merged. Any test
failure you see in an *old* worktree (e.g. a branch based on a stale local `main`
that is ~131 commits behind `origin/main`) is a **staleness artifact**, not a real
bug. Rebase onto `origin/main` before believing a red suite.

> ⚠️ Local `main` on the integrator's machine is stale (131 behind `origin/main`)
> and carries 3 un-pushed commits (diarization polish, self-knowledge docs). These
> are committed, not lost. Do **not** trust `git diff main..`: always compare
> against `origin/main`.

---

## 2. Session / branch status (vs `origin/main`)

| Branch | Session | State | Verdict |
|---|---|---|---|
| `codex/summarizer-brief-scope` | Codex | 0 ahead | ✅ merged, done |
| `claude/fix-cedric-wake-leave` | Claude | 0 ahead | ✅ merged, done |
| `codex/live-turn-fixes` | Codex | in main | ✅ merged (`git cherry` = already upstream) |
| `codex/reliable-fresh-workspace` | Codex | PR #121 | ✅ merged |
| `codex/live-realism-evals` | Codex | in main | ✅ merged (realism fixtures present) |
| `codex/finish-product-loop` | Codex | **4 ahead** | 🟡 **unmerged, high value**, needs PR + review |
| `codex/close-turn-findings` | Codex | PR #123 **draft** | 🟡 owner-driven, finalize + un-draft |
| `claude/hand-raise-etiquette` | Claude | **1 ahead** (`c1a2e66`) | 🟡 **unmerged**: needs PR + review |
| `codex/registry-and-ledger-fixes` | Codex | 0 ahead, dirty | 🔵 live, work uncommitted; leave alone |
| `claude/dash-detail` | **integrator (me)** | WIP | 🔵 coordinator base; holds a preservation commit |

**Pruned this session** (fixes already in `origin/main`, worktrees + branches
removed): `context-pull-fallback`, `fix-dedup-crosslang`, `action-outcome`,
`capture-window-guard`, `fix-test-pollution` (all were leftover `wf_*`/`agent-*`
workflow worktrees).

---

## 3. The only 3 deltas that still carry new value

1. **`codex/finish-product-loop`**: 4 commits, the biggest chunk:
   - `fix(live): preserve leave commands after task capture` (leave-on-command, punch-list #1)
   - `feat(cedric): automate org provisioning and Slack handoff` (registry automation, #2)
   - `fix(dispatch): humanize Recall capacity errors` (Recall 507 → friendly, #3)
   - `refactor(cedric): isolate pending org provisioning`
   - Touches `backend/app/auth.py`, `backend/app/cedric/*`. **Live-contract sensitive.**

2. **`codex/close-turn-findings`** (PR #123, draft): `fix(turn-taking): preserve
   punctuation before trailing emoji`. 4 files incl. `backend/app/end_of_turn.py`
   + tests. Small, well-scoped, has tests.

3. **`claude/hand-raise-etiquette`** (`c1a2e66`): hand-raise + first-call
   activation + `detect_invite`. Touches `main.py`, `decision.py`,
   `config.py`, `recall_client.py`, `store.py` + `test_hand_raise.py`. **Live-path.**
   _(An identical preservation copy is safe on the integrator's `dash-detail` as
   `03c023c`: no work can be lost.)_

---

## 4. Conflict map & recommended merge order

The three are **mostly disjoint** (different subsystems), so conflict risk is low.
Merge low-blast-radius first:

1. **PR #123** (`close-turn-findings`): smallest, test-backed, `end_of_turn.py` only.
2. **`hand-raise-etiquette`**: live path (`main.py`/`decision.py`) but self-contained.
3. **`finish-product-loop`**: largest, Cedric-contract, deploy-sensitive → last, most care.

Only #2 and #3 touch `main.py`; sequence them so the second rebases on the first.

---

## 5. Per-session tickets (what each session must do to be landable)

- **Codex · finish-product-loop:** open a **draft PR** against `main`; confirm the
  registry-automation writes SSM `LAURA_WEBHOOK_SECRETS_BY_ORG` by merge (not
  clobber) and hot-reloads `_secret_for`; add the Recall-507 unit test. Ping the
  integrator for `/code-review` before un-drafting.
- **Codex · close-turn-findings (#123):** finish the emoji-punctuation fix, keep it
  green, **un-draft** when ready. Integrator reviews + lands.
- **Claude · hand-raise-etiquette:** open a **draft PR** for `c1a2e66`; keep the
  gpu/frontend bits out of it (etiquette is backend + `talk.html` gesture only).
- **Codex · registry-and-ledger-fixes:** commit your WIP so it becomes visible;
  check overlap with finish-product-loop's cedric/ledger changes before both land.

---

## 6. Landing checklist (run **per merge**, never batch)

1. **Session guard as its own step:** `GET /health` → `active_sessions == 0`
   **and** `aws apprunner list-operations` on `laura-backend` not `IN_PROGRESS`.
   Read the result, _then_ act. A live meeting racing a deploy has bitten us before.
2. `/code-review` (or `code-reviewer` agent) on any `backend/app/**` change -
   contract, latency, PII, meter safety.
3. Merge **one** branch → wait for App Runner `RUNNING` → then the next. App Runner
   serializes deploys and errors on a concurrent op.
4. Open PRs as `--draft` (owner marks ready) to avoid Copilot+Codex review spam.
