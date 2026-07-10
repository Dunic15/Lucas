#!/usr/bin/env bash
# MuseTalk — one-shot setup + offline test on a Runpod GPU pod.
#
# Prereqs: a Runpod pod (RTX 4090 24GB is enough), and the two input files
# uploaded to /workspace/ :  laura_face.jpg  and  laura_voice.wav
#
# Run:   cd /workspace && bash musetalk_runpod_setup.sh
# Re-run just the inference after tuning bbox_shift:  (see RUN block at the end)
#
# Two reliability fixes baked in (these are what usually break a MuseTalk install):
#   1. mmcv installed from the OpenMMLab PREBUILT wheel (not compiled from source).
#   2. weights pulled from the real huggingface.co (repo default is a CN mirror).
set -euo pipefail
WORK=/workspace
cd "$WORK"

echo "==> [0/6] check input files are uploaded"
for f in laura_face.jpg laura_voice.wav; do
  [ -f "$WORK/$f" ] || { echo "!! MISSING $WORK/$f — upload it to /workspace first"; exit 1; }
done

echo "==> [1/6] system deps (ffmpeg, git)"
apt-get update -y && apt-get install -y ffmpeg git

echo "==> [2/6] torch 2.0.1 (CUDA 11.8)"
pip install torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2 \
  --index-url https://download.pytorch.org/whl/cu118

echo "==> [3/6] clone MuseTalk + python deps"
[ -d MuseTalk ] || git clone https://github.com/TMElyralab/MuseTalk
cd "$WORK/MuseTalk"
pip install -r requirements.txt

echo "==> [4/6] mmlab (prebuilt mmcv wheel — avoids the slow/failing source build)"
pip install -U openmim
mim install mmengine
pip install "mmcv==2.0.1" -f https://download.openmmlab.com/mmcv/dist/cu118/torch2.0/index.html
mim install "mmdet==3.1.0"
mim install "mmpose==1.1.0"

echo "==> [5/6] download weights (~15GB, from real huggingface.co)"
sed -i 's|hf-mirror.com|huggingface.co|g' download_weights.sh || true
sh ./download_weights.sh
export FFMPEG_PATH="$(dirname "$(which ffmpeg)")"

echo "==> [6/6] point the config at OUR face + voice"
mkdir -p configs/inference
cat > configs/inference/test.yaml <<YAML
task_0:
  video_path: "$WORK/laura_face.jpg"
  audio_path: "$WORK/laura_voice.wav"
  bbox_shift: 0
YAML

# ── RUN ── (re-run from here after editing bbox_shift in the yaml above)
echo "==> RUN inference"
sh inference.sh v1.5 normal

echo "==> make a 720p, 15fps copy (== what the meeting delivers when Laura speaks)"
OUT="$(ls -t results/test/*.mp4 | head -1)"
ffmpeg -y -i "$OUT" -vf "scale=-2:720" -r 15 "$WORK/MuseTalk/results/laura_720p.mp4"

echo ""
echo "================= DONE ================="
echo "Full quality : $OUT"
echo "Meeting (720p): $WORK/MuseTalk/results/laura_720p.mp4  <-- guarda QUESTO"
echo "Tip: se la bocca si muove poco, alza bbox_shift (+5/+10) nel test.yaml e rilancia 'sh inference.sh v1.5 normal'."
