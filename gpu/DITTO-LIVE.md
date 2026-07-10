# Ditto LIVE — dal ritratto approvato a Laura in riunione

Stato: **adapter + engine pronti** (2026-07-09). Ditto è la faccia di produzione
(owner-approved: clip `ditto_720p.mp4`). Questo runbook porta quel modello dal
"clip offline" al "Laura parla in Google Meet".

Architettura (tutta già esistente salvo l'engine):
```
backend App Runner (brain+TTS) ──{type:speak, audio}──▶ frontend/photoreal.html
                                                            │ ws {speak, audio_b64}
                                                            ▼
                              pod Runpod: gpu/server.py AVATAR_ENGINE=ditto
                              (ditto_adapter.py → StreamSDK online, TRT)
                                                            │ JPEG frames
                                                            ▼
                                    photoreal.html li dipinge → Recall = camera bot
```

## Fase A — immagine Docker (una tantum, dal tuo Mac)
```bash
cd ~/Desktop/Laura
# sostituisci il ritratto placeholder con quello di produzione approvato
cp ~/Desktop/laura-runpod-test/laura_face.jpg gpu/assets/reference.jpg

docker build -f gpu/Dockerfile.ditto -t <TUO_REGISTRY>/laura-ditto:v1 gpu/
docker push <TUO_REGISTRY>/laura-ditto:v1
```
(Serve un registry: Docker Hub gratuito basta. Su Mac Apple-Silicon aggiungi
`--platform linux/amd64` al build.)

## Fase B — pod Runpod dall'immagine
1. Runpod → Templates → New: image `<TUO_REGISTRY>/laura-ditto:v1`,
   **Expose HTTP Port 8080**, GPU **Ampere+ 24GB** (3090 / A5000; L40S se vuoi margine).
2. Deploy pod dal template → Runpod dà l'URL proxy TLS:
   `https://<POD_ID>-8080.proxy.runpod.net`
3. Verifica: `curl https://<POD_ID>-8080.proxy.runpod.net/health`
   → `{"ok":true,"engine":"ditto",...}` (il primo boot fa il warmup TRT: ~1-2 min).

## Fase C — puntare Laura (solo env sul backend App Runner)
```
AVATAR_PAGE=photoreal
GPU_STREAM_URL=wss://<POD_ID>-8080.proxy.runpod.net/stream
GPU_INSTANCE_ID=          (vuoto! bypassa la logica EC2/AWS)
```
Rollback = rimettere `AVATAR_PAGE=talk` (il 3D resta il fallback gratuito, e la
pagina photoreal degrada da sola a ritratto statico → /talk se il GPU è giù).

## Fase D — smoke test (checklist)
1. `/health` ok, poi apri
   `https://<backend>/photoreal?conversation_id=test&stream_url=wss://<POD>.../stream`
   → devi vedere il ritratto in "idle" (respiro).
2. Da `/demo/ask` falle dire una frase → labbra+testa si muovono, audio in sync.
3. `/metrics` sul pod: `first_frame_ms_avg` **< 1000**, `fps_actual` ≥ 15.
4. Meeting vero: crea una Meet, invita Laura (bot Recall) → la camera del bot è
   la faccia Ditto. Parla con lei. Latency budget: primo suono < 2s.
5. FINE TEST: **termina il pod** (o lascia l'idle-watchdog: env
   `GPU_IDLE_SHUTDOWN_MINUTES=10` + `GPU_SHUTDOWN_CMD="kill 1"` — container
   esce → pod si ferma. NB: VERIFICARE su Runpod che il pod fermo non riparta
   da solo: restart policy "no").

## COLLAUDO 2026-07-10 — risultati misurati (RTX 3090 e 4090, engine portabili)
✅ **Il giro live FUNZIONA end-to-end**: ws `/stream` → speak(mp3) → talk_start →
frame Ditto → talk_end. Adapter validato su GPU vera.
- **first_frame (server-side): 180-410ms** — ECCELLENTE, ampiamente nel budget.
- **Generazione: ~11-12 fps** su ENTRAMBE 3090 e 4090 (identiche!) → il collo
  NON è la potenza GPU: sono gli **engine TRT "Ampere_Plus"** (hardware-compat
  mode = niente ottimizzazioni native) e/o l'overhead Python della pipeline.
- **Bug trovato e fixato**: frame stantii del turno precedente sbavavano nel
  successivo (labbra sfasate) → flush della coda a inizio frase (in adapter).
- Fix d'ambiente consolidati (tutti nel Dockerfile): cudnn8 in prefisso dedicato,
  `cuda-python<12`, patch `np.atan2→arctan2`, tensorrt via pypi.nvidia.com.
- `sampling_timesteps` via kwarg di setup: NESSUN effetto misurato (probabilmente
  ignorato) — non conta come leva finché non si verifica nel cfg pkl.
- Ridurre il ritratto (1122→800px) non cambia gli fps (costo nelle fasi interne).

## ⭐ RISOLTO (2026-07-10, sessione due): erano 23fps VERI — il "11fps" era un artefatto
La sessione strumentata (A4500 $0.19/h) ha smontato il mistero in tre atti:
1. wav2feat/HuBERT: solo ~22ms/chunk → INNOCENTE (ipotesi smentita dai dati).
2. Il writer riceve frame ogni ~30ms (≈33fps di produzione), encode 8ms.
3. **Il colpevole del "11fps": un timeout di coda da 10s** a fine clip
   (off-by-one tra frame attesi e prodotti) che gonfiava OGNI misura — e
   essendo una costante, rendeva "identiche" tutte le GPU. FIXATO
   nell'adapter (attesa corta post-feeder) + client che misura fino
   all'ULTIMO frame.
**Numeri di produzione reali (A4500 da $0.19/h): ~23fps un volto, ~17fps col
secondo volto caricato. Recall consegna comunque max 15fps → GIÀ SOPRA IL
TETTO del meeting.** Drift residuo ~0.7s su frasi da 8.5s (impercettibile
sulle risposte tipiche 3-5s). Rifinitura opzionale per il 25 pieno: knob
sampling_timesteps (via cfg, non kwarg — ignorato lì), CPU del pod più larga.

## MULTI-VOLTO (stesso pod) — FUNZIONA ✅
`REFERENCE_IMAGES="laura:/x/laura.jpg,cedric:/x/cedric.jpg"` sul server → un
engine per volto (~2.6GB VRAM l'uno); la pagina photoreal passa
`?avatar_id=` sul ws e riceve il volto giusto (hello.face conferma).
Retrocompatibile: senza env resta il singolo REFERENCE_IMAGE. Testato live:
Laura e Cedric serviti dalla stessa A4500. Nota: due volti attivi → ~17fps
cad. (contesa CPU); per il massimo fps: un pod per riunione comunque.

## Archivio prima sessione (metodo) — il collo NON era la GPU
Misure a parità di adapter (~11-12fps SEMPRE):
| Config | fps |
|---|---|
| RTX 3090, engine portabili | 12.1 |
| RTX 4090, engine portabili | 11.3 |
| RTX 4090, engine NATIVI (cvt_onnx_to_trt: fatto, funziona) | 11.7 |
| **A100-SXM4-80GB**, engine nativi-Ampere | **10.8** |
3× la potenza, 2× la banda → **zero differenza**. Il limite è un passo a COSTO
FISSO nella pipeline software, non l'hardware.

**Indiziato principale: `wav2feat` (HuBERT)** — chiamato PER-CHUNK dentro
run_chunk, verosimilmente su CPU: ~450ms per chunk da 5 frame ≈ 11fps, identico
su ogni GPU. Fix candidati (prossima sessione, su un 3090 ECONOMICO — tanto
l'hardware non c'entra):
1. Instrumentare i tempi per stage (wav2feat vs code) e confermare.
2. **Batch HuBERT sull'intera frase** in un colpo solo (stile offline) invece
   che per-chunk — o HuBERT su GPU. Se confermato, anche il 3090 da $0.22/h
   può fare ≥25fps → costo live resta ~$0.02/min. 🎯
3. In alternativa/parallelo: profilare con py-spy (serve --cap-add SYS_PTRACE
   o py-spy da root fuori dal processo; nel container standard non funziona).
NB: finché la generazione è <25fps, l'A/V si sfasa su frasi lunghe — per demo
brevi (5-8s) è modesto ma visibile sul finale. First-frame resta ottimo
(180-550ms) su tutte le config.

## Poi (fase 2 del live)
- **Spin-per-meeting**: agganciare start/stop del pod al calendario Recall
  (webhook già in piedi per l'auto-join) via API Runpod — stessa filosofia di
  `gpu_runtime.py` ma control-plane Runpod al posto di EC2. Fino ad allora:
  accendi/spegni a mano dalle dashboard (o `runpodctl`).
- **Multi-tenant**: N riunioni su una GPU (il ~$0.50 → ~$0.10/riunione).
- Costi live: pod acceso solo in riunione ≈ **$0.20-0.50 / meeting 30min**.

## Nota meter (regola d'oro, come Recall)
GPU accesa = soldi. Tre reti di sicurezza: (1) idle-watchdog nel server,
(2) terminazione manuale post-test, (3) budget alert Runpod (Settings →
Billing). Mai lasciare un pod "per comodità".
