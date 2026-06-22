#!/bin/bash
# Re-train an ACT model from its EXISTING LeRobot dataset (no reconvert) — e.g. to
# restore a model whose checkpoints were deleted. Hardened against the torchcodec
# SIGILL (pyav + fewer workers), checkpoints every 10k, auto-resumes on any crash.
# Logs to logs/restore/ so the GUI shows it as a live training.
# Usage: retrain_from_dataset.sh <repo_short>     e.g.  bottle_rgbd_v1
ACT="$HOME/miniconda3/envs/act_train/bin/python"
cd "$HOME/Desktop/g1_pico_wbc_console" || exit 1
R="${1:?usage: retrain_from_dataset.sh <repo_short>}"
REPO="g1_pico_act/$R"
OUT="training/g1_pico_act_$R"
mkdir -p logs/restore
LOG="logs/restore/act_train__g1_pico_act_$R.log"
echo "=== retrain $REPO (no reconvert) $(date +%T) ===" >> "$LOG"
for attempt in $(seq 1 8); do
  LAST=$(ls "$OUT/checkpoints" 2>/dev/null | grep -E '^[0-9]+$' | sort -n | tail -1)
  if [ "$LAST" = "100000" ]; then echo "[$R] ✅ 100k done"; break; fi
  if [ -d "$OUT/checkpoints/last" ]; then
    CFG="$OUT/checkpoints/last/pretrained_model/train_config.json"
    echo "[$R] attempt $attempt: resume from step ${LAST}  $(date +%T)"
    "$ACT" -m lerobot.scripts.lerobot_train --config_path="$CFG" --resume=true \
      --num_workers=2 --save_freq=10000 --dataset.video_backend=pyav >> "$LOG" 2>&1
  else
    echo "[$R] attempt $attempt: fresh train from existing dataset  $(date +%T)"
    "$ACT" -m lerobot.scripts.lerobot_train \
      --dataset.repo_id="$REPO" --policy.type=act --policy.push_to_hub=false \
      --steps=100000 --output_dir="$OUT" \
      --num_workers=2 --save_freq=10000 --dataset.video_backend=pyav >> "$LOG" 2>&1
  fi
  rc=$?
  if [ $rc -eq 0 ]; then echo "[$R] ✅ done"; break; fi
  echo "[$R] crashed rc=$rc — auto-retry/resume…"; sleep 5
done
echo "=== retrain $R COMPLETE $(date +%T) ==="
