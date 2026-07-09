# Runpod offline test — foto + voce → clip parlante (MuseTalk)

Obiettivo: **vedere la qualità** del volto photoreal di Laura che parla, con la sua
voce ElevenLabs, PRIMA di costruire il live. È un job batch: dai a MuseTalk la foto +
l'audio, ti sputa un video. Nessun endpoint, nessun adapter — solo qualità da valutare.

**Input pronti** (sul desktop, cartella `laura-runpod-test/`):
- `laura_face.jpg` — il ritratto AI approvato (1122×1402)
- `laura_voice.wav` — voce ElevenLabs di Laura, 8.5s, 16kHz mono (dal backend `/tts`)

**Costo/tempo:** GPU 24GB (RTX 4090 ~$0.34–0.69/h). ~1–2h in tutto (gran parte è il
download dei pesi ~15GB) → **~$1–2**.

---

## 1. Crea il pod
Runpod → **Deploy** → GPU **RTX 4090 (24GB)** → template **"RunPod PyTorch 2.x"**
(CUDA preinstallato). Metti **Container Disk ≥ 40GB** (i pesi sono ~15GB). Deploy →
**Connect** → apri **Jupyter Lab** o **Web Terminal**.

## 2. Carica i 2 file
Nel file-manager di Jupyter Lab, trascina `laura_face.jpg` e `laura_voice.wav` in
`/workspace/`. *(In alternativa da locale: `runpodctl send laura-runpod-test/` e sul pod
`runpodctl receive <code>`.)*

## 3. Installa MuseTalk (il pezzo un po' fiddly — vai con calma qui)
```bash
cd /workspace
apt-get update && apt-get install -y ffmpeg git
git clone https://github.com/TMElyralab/MuseTalk
cd MuseTalk

# ambiente (python 3.10 + torch cu118)
pip install torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2 --index-url https://download.pytorch.org/whl/cu118
pip install -r requirements.txt

# mmlab (la parte che si rompe più spesso — installa in QUEST'ordine)
pip install -U openmim
mim install mmengine
mim install "mmcv==2.0.1"
mim install "mmdet==3.1.0"
mim install "mmpose==1.1.0"

export FFMPEG_PATH=$(dirname $(which ffmpeg))
```

## 4. Scarica i pesi (~15GB)
```bash
sh ./download_weights.sh
# deve popolare ./models/ con: musetalkV15/unet.pth, sd-vae/, whisper/, dwpose/, face-parse-bisent/
ls -R models | head
```

## 5. Configura l'input
Scrivi il config puntando ai NOSTRI file:
```bash
cat > configs/inference/test.yaml <<'YAML'
task_0:
  video_path: "/workspace/laura_face.jpg"
  audio_path: "/workspace/laura_voice.wav"
  bbox_shift: 0
YAML
```
`bbox_shift`: regola quanto si apre la bocca. Parti da 0; se la bocca si muove poco alza
(+5/+10), se esagera abbassa (−5). Un paio di run per tararlo.

## 6. Lancia l'inferenza
```bash
sh inference.sh v1.5 normal
# se lo script non c'è, comando esplicito equivalente:
python -m scripts.inference \
  --inference_config configs/inference/test.yaml \
  --result_dir results/test \
  --unet_model_path models/musetalkV15/unet.pth \
  --unet_config models/musetalkV15/musetalk.json \
  --version v15 \
  --ffmpeg_path $FFMPEG_PATH
```
Il video esce in `results/test/` (mp4).

## 7. Rendilo rappresentativo del meeting (720p) e scaricalo
```bash
# la clip com'è = qualità piena; questa è la versione "come si vede in riunione" (720p)
ffmpeg -y -i results/test/*.mp4 -vf "scale=-2:720" -r 15 results/laura_720p.mp4
```
Scarica `results/test/*.mp4` (piena) **e** `results/laura_720p.mp4` (720p @15fps = ciò che
Recall consegna quando Laura parla). Guarda la 720p per giudicare onestamente.

## 8. Spegni il pod
Runpod → **Stop**/**Terminate** (paghi finché è acceso).

---

## Cosa valutare
- Il **lip-sync** è credibile? La bocca segue la voce?
- Il volto sembra **una persona vera** (vs il cartone 3D attuale)?
- A **720p** regge? (è quello che conta per il meeting)

Nota: MuseTalk su foto ferma muove **solo la bocca** (testa immobile → può sembrare un po'
statico). Se vogliamo più "vita" (testa/espressioni che si muovono), lo step successivo è
**Ditto** — stessa pipeline, aggiunge il movimento della testa.

## Se la qualità convince → il live
Riusa TUTTO: stessa foto, stessa voce, stesso modello. Si aggiunge solo `musetalk_adapter.py`
(bridge realtime) + il server `/stream` sul pod, e Laura punta lì via env
(`AVATAR_PAGE=photoreal`, `GPU_STREAM_URL=wss://<pod>/stream`).
