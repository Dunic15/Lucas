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

## Per arrivare a ≥25fps (prossima sessione di tuning, in ordine)
1. **Ricompilare gli engine TRT NATIVI per la GPU target** — la leva più
   promettente: `python scripts/cvt_onnx_to_trt.py --onnx_dir ./checkpoints/ditto_onnx
   --trt_dir ./checkpoints/ditto_trt_custom` (serve scaricare anche `ditto_onnx/*`,
   build ~10-30 min sul pod), poi `data_root=ditto_trt_custom`.
2. **Profilare la pipeline python** (py-spy da fuori container / con SYS_PTRACE):
   se è GIL-bound, gli engine nativi non basteranno — servono i knob di coda.
3. **Tier datacenter** (A100/H100): Ditto dichiara RTF 0.89 su A100 — quasi
   certamente ≥25fps lì ($1.4-1.9/h → comunque ~$0.05/min a riunione).
4. **JPEG_QUALITY**: 90 regge (3-6 Mbit a 720p/25).
NB: finché la generazione è <25fps, l'A/V si sfasa su frasi lunghe — per demo
brevi (frasi ~5-8s) lo sfasamento è modesto ma visibile sul finale.

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
