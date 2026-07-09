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

## Tuning atteso (sessione launch-day — è normale)
- **Chunk pacing**: `DITTO_CHUNK` (default 3,5,2). Chunk più corti = prima
  parola più veloce, più overhead.
- **first_frame_ms alto** → controlla che il warmup silenzioso sia andato
  (log "engine=ditto"), e prova `STREAM_FPS=20`.
- **fps_actual < 15 su GPU piccola** → L40S, o TensorRT engines ricostruiti
  per l'arch esatta (`scripts/cvt_onnx_to_trt.py` nel repo Ditto).
- **JPEG_QUALITY**: 90 di default (lezione: 82 era conservativo; la banda del
  proxy regge 90 a 720p/25fps ≈ 3-6 Mbit).

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
