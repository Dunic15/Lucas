#!/usr/bin/env bash
# MuseTalk su Clariden (GH200 / aarch64) — setup + inferenza offline in UN job.
# Gira DENTRO il container EDF "musetalk" (NGC pytorch, arm64), lanciato da sbatch.
#
# Input attesi in $WORK (default: /iopsstor/scratch/cscs/$USER/musetalk):
#   laura_face.jpg   laura_voice.wav
# Output:  $WORK/results/laura_full.mp4  e  $WORK/results/laura_720p.mp4
#
# Differenze vs la versione Runpod (x86):
#   1. NIENTE pip install torch (si usa quello dell'immagine NGC arm64).
#   2. mmcv NON ha wheel arm64 -> si COMPILA da sorgente (la parte lenta/fragile;
#      sul Grace a 72+ core ~10-25 min con MAX_JOBS alto).
#   3. ffmpeg via imageio-ffmpeg (binario statico arm64) — niente apt nel container.
set -eo pipefail
export WORK="${WORK:-/iopsstor/scratch/cscs/$USER/musetalk}"
mkdir -p "$WORK" && cd "$WORK"

echo "==> [0/6] GPU e input"
nvidia-smi -L
python3 -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
for f in laura_face.jpg laura_voice.wav; do
  [ -f "$WORK/$f" ] || { echo "!! manca $WORK/$f — fai lo scp prima (vedi README)"; exit 1; }
done

echo "==> [1/6] clone MuseTalk + deps python (SENZA torch)"
[ -d MuseTalk ] || git clone https://github.com/TMElyralab/MuseTalk
cd "$WORK/MuseTalk"
# requirements senza righe torch/tensorflow (pesanti/x86-centric; tensorflow non
# serve all'inferenza — è nel requirements per il training)
grep -viE '^(torch|torchvision|torchaudio|tensorflow|tensorboard)' requirements.txt > /tmp/req.txt
pip install --no-cache-dir -r /tmp/req.txt
pip install --no-cache-dir "huggingface_hub[cli]" imageio-ffmpeg

echo "==> [2/6] ffmpeg statico (imageio) su PATH"
FF="$(python3 -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
mkdir -p "$WORK/bin" && ln -sf "$FF" "$WORK/bin/ffmpeg"
export PATH="$WORK/bin:$PATH" FFMPEG_PATH="$WORK/bin"
ffmpeg -version | head -1

echo "==> [3/6] mmlab: mmcv da SORGENTE (arm64), mmdet/mmpose (pure-python)"
pip install --no-cache-dir -U openmim && mim install mmengine
python3 - <<'PY' || NEED_BUILD=1
import mmcv  # già presente da un run precedente?
print("mmcv già installato:", mmcv.__version__)
PY
if [ "${NEED_BUILD:-0}" = "1" ]; then
  export MMCV_WITH_OPS=1 MAX_JOBS="${MAX_JOBS:-64}" FORCE_CUDA=1
  pip install --no-cache-dir "mmcv==2.1.0" --no-binary mmcv   # 2.1.0: compatibile coi torch recenti delle NGC
fi
mim install "mmdet==3.2.0" "mmpose==1.3.1"   # niente ops compilate: ok su arm64

echo "==> [4/6] pesi (~15GB) da huggingface.co su scratch"
sed -i 's|hf-mirror.com|huggingface.co|g' download_weights.sh || true
sh ./download_weights.sh
ls models/musetalkV15/unet.pth >/dev/null && echo "pesi ok"

echo "==> [5/6] config: la NOSTRA foto + voce"
mkdir -p configs/inference
cat > configs/inference/test.yaml <<YAML
task_0:
  video_path: "$WORK/laura_face.jpg"
  audio_path: "$WORK/laura_voice.wav"
  bbox_shift: 0
YAML

echo "==> [6/6] inferenza"
sh inference.sh v1.5 normal

mkdir -p "$WORK/results"
OUT="$(ls -t results/test/*.mp4 | head -1)"
cp "$OUT" "$WORK/results/laura_full.mp4"
# copia 720p@15fps == cio' che consegna il meeting quando Laura parla
ffmpeg -y -i "$OUT" -vf "scale=-2:720" -r 15 "$WORK/results/laura_720p.mp4"

echo "================= FATTO ================="
echo "Full : $WORK/results/laura_full.mp4"
echo "720p : $WORK/results/laura_720p.mp4   <-- guarda QUESTO"
echo "Se la bocca si muove poco: alza bbox_shift (+5/+10) nel test.yaml e rilancia solo lo step [6]."
