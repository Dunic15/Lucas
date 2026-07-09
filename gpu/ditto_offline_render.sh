#!/usr/bin/env bash
# Render offline Ditto: foto + voce -> clip parlante (testa+espressioni vive).
# Consolida TUTTE le lezioni del primo collaudo (2026-07-09) in un colpo solo:
#   numpy<2 SEMPRE per ultimo · onnxruntime-gpu · mediapipe · libGLES/EGL
#   pesi: solo ditto_cfg+ditto_pytorch · hub pinnato 0.30.2
#
# Uso su un pod Runpod (immagine runpod/pytorch:2.1.0-py3.10-cuda11.8.0):
#   1) carica in /workspace:  <SUBJECT>_face.jpg  <SUBJECT>_voice.wav  questo script
#   2) SUBJECT=cedric bash ditto_offline_render.sh
# Output: /workspace/<SUBJECT>_ditto_full.mp4 + <SUBJECT>_ditto_720p.mp4
set -eo pipefail
SUBJECT="${SUBJECT:-laura}"
FACE="/workspace/${SUBJECT}_face.jpg"
VOICE="/workspace/${SUBJECT}_voice.wav"
cd /workspace
for f in "$FACE" "$VOICE"; do [ -f "$f" ] || { echo "!! manca $f"; exit 1; }; done

echo "==> [1/6] system deps (ffmpeg, git, GLES/EGL per mediapipe headless)"
apt-get update -y >/dev/null
apt-get install -y -q ffmpeg git libgl1 libglib2.0-0 libgles2 libegl1 libopengl0 >/dev/null

echo "==> [2/6] repo + deps python (numpy pinnato PER ULTIMO — vince su tutto)"
[ -d ditto-talkinghead ] || git clone --depth 1 https://github.com/antgroup/ditto-talkinghead
cd /workspace/ditto-talkinghead
pip install --no-cache-dir -q librosa tqdm filetype imageio imageio-ffmpeg \
  opencv_python_headless scikit-image cython cuda-python colored polygraphy \
  einops mediapipe "onnxruntime-gpu==1.18.1" "huggingface_hub[cli]==0.30.2"
pip install --no-cache-dir -q "numpy==1.26.4"
python3 -c "import numpy, torch; assert numpy.__version__.startswith('1.'), 'numpy2!'; print('numpy', numpy.__version__, '| cuda', torch.cuda.is_available())"

echo "==> [3/6] pesi (ditto_cfg + ditto_pytorch; se HF da 429 -> vedi runbook: stream da locale)"
huggingface-cli download digital-avatar/ditto-talkinghead \
  --include "ditto_cfg/*" "ditto_pytorch/*" --local-dir checkpoints
du -sh checkpoints

echo "==> [4/6] foto -> png"
python3 - "$FACE" <<'PY'
import sys
from PIL import Image
Image.open(sys.argv[1]).convert("RGB").save("/workspace/_face.png")
PY

echo "==> [5/6] RUN Ditto (pytorch path)"
python3 inference.py \
  --data_root "./checkpoints/ditto_pytorch" \
  --cfg_pkl "./checkpoints/ditto_cfg/v0.4_hubert_cfg_pytorch.pkl" \
  --audio_path "$VOICE" \
  --source_path "/workspace/_face.png" \
  --output_path "/workspace/${SUBJECT}_ditto_full.mp4"

echo "==> [6/6] copia 720p@15fps (qualita-meeting)"
ffmpeg -y -v error -i "/workspace/${SUBJECT}_ditto_full.mp4" -vf "scale=-2:720" -r 15 \
  "/workspace/${SUBJECT}_ditto_720p.mp4"
ls -la /workspace/${SUBJECT}_ditto_*.mp4
echo "================= DONE ================="
