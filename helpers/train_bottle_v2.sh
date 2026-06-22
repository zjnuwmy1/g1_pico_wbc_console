#!/bin/bash
# Wait for the bottle_v2 dataset build to finish, then train the v2 model WITH
# image augmentation (the Phase-1 A/B candidate). Hardened (pyav, save 10k,
# auto-resume). Logs to logs/restore/ so the GUI shows it as a live training.
ACT="$HOME/miniconda3/envs/act_train/bin/python"
cd "$HOME/Desktop/g1_pico_wbc_console" || exit 1
DS="$HOME/.cache/huggingface/lerobot/g1_pico_act/bottle_v2"
OUT="training/g1_pico_act_bottle_v2"
mkdir -p logs/restore
LOG="logs/restore/act_train__g1_pico_act_bottle_v2.log"

echo "[v2] waiting for dataset build…"
while pgrep -f "[b]uild_act_dataset.*bottle_v2" >/dev/null; do sleep 5; done
EP=$("$ACT" -c "import json;print(json.load(open('$DS/meta/info.json'))['total_episodes'])" 2>/dev/null)
echo "[v2] dataset built: ${EP} episodes"
[ "${EP:-0}" -ge 100 ] 2>/dev/null || { echo "[v2] build incomplete (${EP}); abort"; exit 1; }
[ -d "$OUT" ] && rm -rf "$OUT"

for attempt in $(seq 1 8); do
  LAST=$(ls "$OUT/checkpoints" 2>/dev/null | grep -E '^[0-9]+$' | sort -n | tail -1)
  [ "$LAST" = "100000" ] && { echo "[v2] ✅ 100k done"; break; }
  START=$(date +%s)
  if [ -d "$OUT/checkpoints/last" ]; then
    CFG="$OUT/checkpoints/last/pretrained_model/train_config.json"
    echo "[v2] attempt $attempt resume from ${LAST} $(date +%T)"
    "$ACT" -m lerobot.scripts.lerobot_train --config_path="$CFG" --resume=true \
      --num_workers=2 --save_freq=10000 --dataset.video_backend=pyav >> "$LOG" 2>&1
  else
    echo "[v2] attempt $attempt fresh train (v2 layout + image aug) $(date +%T)"
    "$ACT" -m lerobot.scripts.lerobot_train \
      --dataset.repo_id=g1_pico_act/bottle_v2 --policy.type=act --policy.push_to_hub=false \
      --steps=100000 --output_dir="$OUT" \
      --num_workers=2 --save_freq=10000 --dataset.video_backend=pyav \
      --dataset.image_transforms.enable=true >> "$LOG" 2>&1
  fi
  rc=$?; DUR=$(( $(date +%s) - START ))
  [ $rc -eq 0 ] && { echo "[v2] ✅ done"; break; }
  [ $DUR -lt 60 ] && { echo "[v2] ✗ failed in ${DUR}s rc=$rc — cmd/flag error, see $LOG"; tail -5 "$LOG"; break; }
  echo "[v2] crash rc=$rc after ${DUR}s — auto-resume…"; sleep 5
done
echo "=== bottle_v2 train COMPLETE $(date +%T) ==="
